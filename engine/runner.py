from __future__ import annotations

import asyncio
import json
import signal
import uuid
from pathlib import Path
from typing import Any

import httpx

from clonoth_runtime import (
    fetch_openai_secret,
    get_bool,
    get_float,
    get_int,
    get_str,
    load_runtime_config,
    normalize_openai_secret,
)
from providers.openai import OpenAIProvider
from toolbox.context import ToolContext
from toolbox.registry import ToolRegistry

from .inference import run_ai_node
from .context import RunContext
from .model import resolve_provider
from .context_store import load_context_snapshot

from .node import Node, load_node
# [2026-04-17] write_artifact 移除：截断机制已废弃
from .tool_step import result_to_raw, summarize_result
# Phase 1 (Session Conversation Store): 导入 ConversationStore 用于影子写入，
# 在每个 node task 执行时实例化并挂载到 RunContext，供 ai_step 影子写入消息。
from .conversation_store import ConversationStore, Message, MessageType
# Phase 0/1: Signal System — 导入信号总线初始化和桥接函数。
# 在 _run_node_task 中初始化 bus 并安装 EventLog 桥接，使 LLM 调用信号
# 自动转发到 data/signals.jsonl 供监控使用。
from engine.signals import get_bus
from engine.signals.bridge import install_event_bridge


def _collect_node_info(workspace_root: Path, node_ids: list[str]) -> list[dict[str, str]]:
    """收集指定节点的基本信息（id/name/description）。"""
    result: list[dict[str, str]] = []
    for target_id in node_ids:
        target = load_node(workspace_root, target_id)
        if target is None:
            continue
        result.append({
            "id": target.id,
            "name": target.name,
            "description": target.description or target.name,
        })
    return result


def _discover_switchable_nodes(workspace_root: Path, current_node_id: str) -> list[dict[str, str]]:
    """发现可切换的根节点（不被任何其他节点 delegate_targets 引用的节点），排除当前节点。"""
    nodes_dir = workspace_root / "config" / "nodes"
    if not nodes_dir.is_dir():
        return []
    all_nodes: list[dict[str, Any]] = []
    all_targets: set[str] = set()
    for f in sorted(nodes_dir.iterdir()):
        if f.suffix not in (".yaml", ".yml") or f.name.startswith("_"):
            continue
        n = load_node(workspace_root, f.stem)
        if n is None or n.type != "ai":
            continue
        all_nodes.append({"id": n.id, "name": n.name, "description": n.description or n.name})
        all_targets.update(n.delegate_targets)
    # 根节点 = 不被任何节点 delegate 引用的节点
    roots = [n for n in all_nodes if n["id"] not in all_targets]
    if not roots:
        roots = list(all_nodes)
    # 排除当前节点自己
    return [n for n in roots if n["id"] != current_node_id]


_PSEUDO_TOOL_NAMES = {"finish", "dispatch_node", "dispatch_nodes", "reply", "switch_node"}


def _message_to_history_dict(msg: Message) -> dict[str, Any]:
    """将 ConversationStore 的 Message 转为 runner 期望的 history dict 格式。

    Child Session 隔离（Phase B）：从 child session JSONL 加载的 Message 对象
    需要转为与 snapshot messages 相同的 dict 格式，供 ai_step 的消息组装使用。
    """
    d: dict[str, Any] = {"role": msg.role, "content": msg.content}
    # [缺陷修复] ConversationStore 加载的历史消息可能包含多模态 content（list 类型），
    # 其中的 file:// 图片引用在 24h 后会因 data_cleanup 清理附件文件而失效。
    # 与旧的 _fetch_history 路径保持一致，在加载时就剥离图片引用为纯文本占位符。
    if isinstance(d["content"], list):
        d["content"] = _strip_images_from_content(d["content"])
    if msg.meta:
        d["_meta"] = msg.meta
    return d


def _strip_trailing_pseudo_call(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip trailing pseudo-tool call from the last assistant message in history.

    When restoring history from a context snapshot, the last assistant message
    may contain a pseudo-tool record like '[Tool call history record: finish was executed with args: {...}]'.
    For finish: extract the text param and replace with a normal assistant reply.
    For dispatch_node/dispatch_nodes: drop or trim.
    """
    if not history:
        return history

    last = history[-1]
    role = last.get("role", "")
    if role not in ("assistant", "user"):
        return history

    content = last.get("content", "")
    if not isinstance(content, str):
        return history

    # Look for pseudo-tool record marker
    pseudo_name = ""
    marker_pos = -1
    for name in _PSEUDO_TOOL_NAMES:
        tag = f"[Tool call history record: {name} was executed with args: "
        pos = content.find(tag)
        if pos >= 0:
            pseudo_name = name
            marker_pos = pos
            break

    if marker_pos < 0:
        return history

    pre_text = content[:marker_pos].strip()
    result = list(history)

    if pseudo_name == "finish":
        # Extract the text param and restore as assistant reply
        call_str = content[marker_pos:]
        args_start = call_str.find("args: ")
        if args_start >= 0:
            inner = call_str[args_start + len("args: "):]
            # Strip trailing ']' bracket
            last_bracket = inner.rfind("]")
            if last_bracket >= 0:
                inner = inner[:last_bracket]
                try:
                    args = json.loads(inner)
                    finish_text = str(args.get("text", "")).strip()
                    if finish_text:
                        combined = f"{pre_text}\n\n{finish_text}".strip() if pre_text else finish_text
                        # Always restore as assistant role (the finish text is the AI's final response)
                        result[-1] = {"role": "assistant", "content": combined}
                        return result
                except Exception:
                    pass

    # ask/dispatch_node or parse failure: keep pre_text or drop the message
    if pre_text:
        result[-1] = {"role": role, "content": pre_text}
        return result
    return result[:-1]


def _strip_images_from_content(content: list[dict[str, Any]]) -> str:
    """Strip image_url parts from multimodal content, return plain text with placeholder.

    Historical images should not be re-sent every turn as base64 (expensive and redundant).
    Current turn's images are passed separately via the attachments mechanism.
    """
    text_parts: list[str] = []
    had_images = False
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            had_images = True
        elif part.get("type") == "text":
            t = str(part.get("text", "")).strip()
            if t:
                text_parts.append(t)
    text = "\n".join(text_parts)
    if had_images:
        text = f"{text}\n[图片附件已省略]" if text else "[图片附件已省略]"
    return text


async def _fetch_history(rctx: RunContext, limit: int = 40) -> list[dict[str, Any]]:
    try:
        r = await rctx.http.get(
            f"{rctx.supervisor_url}/v1/sessions/{rctx.session_id}/messages",
            params={"limit": limit},
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                result = []
                for m in data:
                    if not isinstance(m, dict) or m.get("role") not in {"user", "assistant", "system"}:
                        continue
                    content = m.get("content", "")
                    if isinstance(content, list):
                        # Strip image_url parts from historical messages to avoid
                        # re-encoding large base64 images on every turn.
                        # Current turn's images are passed separately via attachments.
                        result.append({"role": str(m.get("role")), "content": _strip_images_from_content(content)})
                    else:
                        result.append({"role": str(m.get("role")), "content": str(content)})
                return result
    except Exception:
        pass
    return []


async def _register_engine(
    http: httpx.AsyncClient,
    supervisor_url: str,
    worker_id: str,
    generation_id: str,
) -> None:
    """Register this engine instance with supervisor (Direction 2: Generation ID).

    Triggers cleanup of orphaned tasks from previous engine instances.
    """
    try:
        r = await http.post(
            f"{supervisor_url}/v1/engine/register",
            json={"worker_id": worker_id, "generation_id": generation_id},
        )
        if r.status_code == 200:
            data = r.json()
            orphans = data.get("orphans_cancelled", 0)
            if orphans:
                print(f"[engine] registered generation {generation_id[:8]}, cancelled {orphans} orphan(s)", flush=True)
            else:
                print(f"[engine] registered generation {generation_id[:8]}", flush=True)
        else:
            print(f"[engine] registration failed: HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"[engine] failed to register with supervisor: {e}", flush=True)


async def wait_supervisor(
    http: httpx.AsyncClient,
    base_url: str,
    *,
    timeout: float = 2.0,
    interval: float = 0.5,
) -> None:
    print(f"[engine] waiting for supervisor: {base_url}", flush=True)
    while True:
        try:
            r = await http.get(f"{base_url}/v1/health", timeout=timeout)
            if r.status_code == 200:
                print("[engine] supervisor connected", flush=True)
                return
        except Exception:
            pass
        await asyncio.sleep(interval)


async def worker_loop(*, supervisor_url: str, workspace_root: Path, worker_id: str = "") -> None:
    wid = worker_id or str(uuid.uuid4())
    generation_id = str(uuid.uuid4())  # Direction 2: unique generation per engine startup
    runtime_cfg = load_runtime_config(workspace_root)
    registry = ToolRegistry(workspace_root=workspace_root, tools_dir=workspace_root / "tools")
    _last_reload_seq = 0

    sup_timeout = get_float(runtime_cfg, "engine.http.client_timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    llm_timeout = get_float(runtime_cfg, "providers.openai.timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    poll_sec = get_float(runtime_cfg, "engine.poll_interval_sec", 1.0, min_value=0.1, max_value=60.0)
    max_workers = get_int(runtime_cfg, "engine.max_workers", 4, min_value=1, max_value=32)

    async with (
        httpx.AsyncClient(timeout=sup_timeout, trust_env=False, headers={"User-Agent": "Clonoth"}) as http,
        httpx.AsyncClient(timeout=llm_timeout, trust_env=False, headers={"User-Agent": "Clonoth"}) as llm_http,
    ):
        await wait_supervisor(http, supervisor_url)

        # Direction 2: register generation with supervisor, triggering orphan cleanup
        await _register_engine(http, supervisor_url, wid, generation_id)

        mcp_count = await registry.load_mcp_tools()
        if mcp_count:
            print(f"[engine] loaded {mcp_count} MCP tools", flush=True)

        # Direction 1: graceful shutdown via signal handling
        stop_event = asyncio.Event()
        _loop = asyncio.get_running_loop()

        def _request_stop():
            if not stop_event.is_set():
                print("[engine] received stop signal, initiating graceful shutdown...", flush=True)
                stop_event.set()

        for _sig in (signal.SIGTERM, signal.SIGINT):
            try:
                _loop.add_signal_handler(_sig, _request_stop)
            except (NotImplementedError, RuntimeError):
                pass  # Windows or non-main thread

        print(f"[engine] worker {wid[:8]} ready (max_concurrent={max_workers})", flush=True)
        _active: set[asyncio.Task] = set()

        while not stop_event.is_set():
            try:
                # ---- reap finished tasks ----
                _done = {t for t in _active if t.done()}
                for t in _done:
                    if not t.cancelled():
                        _exc = t.exception()
                        if _exc:
                            print(f"[engine] task error: {_exc}", flush=True)
                _active -= _done

                # ---- tools hot-reload check ----
                try:
                    rr = await http.get(f"{supervisor_url}/v1/tools/reload-seq")
                    if rr.status_code == 200:
                        new_seq = int(rr.json().get("seq", 0))
                        if new_seq > _last_reload_seq:
                            _last_reload_seq = new_seq
                            count = registry.reload()
                            print(f"[engine] tools reloaded ({count} tools)", flush=True)
                except Exception:
                    pass

                # ---- poll for new task (only if below capacity) ----
                if len(_active) < max_workers:
                    nr = await http.get(f"{supervisor_url}/v1/tasks/next", params={"worker_id": wid})
                    if nr.status_code == 200:
                        item = nr.json()
                        if isinstance(item, dict) and item.get("task_id"):
                            _t = asyncio.create_task(
                                _handle_task(http, llm_http, supervisor_url, workspace_root, registry, item, wid)
                            )
                            _active.add(_t)
                            continue
            except Exception as e:
                print(f"[engine] error: {e}", flush=True)
            # Stop-aware sleep: wake immediately on stop signal
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_sec)
            except asyncio.TimeoutError:
                pass

        # ---- Direction 1: graceful shutdown — cancel active tasks and write terminal states ----
        if _active:
            print(f"[engine] shutting down, cancelling {len(_active)} active task(s)...", flush=True)
            for t in _active:
                t.cancel()
            # Wait for tasks to handle CancelledError and POST terminal state
            await asyncio.wait(_active, timeout=15.0)
        print(f"[engine] worker {wid[:8]} stopped (generation {generation_id[:8]})", flush=True)


async def _heartbeat(
    http: httpx.AsyncClient,
    sup_url: str,
    task_id: str,
    worker_id: str,
    interval: float = 60.0,
    lease_sec: float = 120.0,
) -> None:
    """Periodically renew task lease to prevent zombie reaping."""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await http.post(
                    f"{sup_url}/v1/tasks/{task_id}/renew_lease",
                    json={"worker_id": worker_id, "lease_sec": lease_sec},
                )
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


async def _handle_task(
    http: httpx.AsyncClient,
    llm_http: httpx.AsyncClient,
    sup_url: str,
    ws_root: Path,
    registry: ToolRegistry,
    item: dict[str, Any],
    worker_id: str,
) -> None:
    task_id = str(item.get("task_id") or "").strip()
    kind = str(item.get("kind") or "").strip()
    session_id = str(item.get("session_id") or "").strip()
    session_generation = int(item.get("session_generation") or 0)

    cfg_raw = await fetch_openai_secret(http, sup_url)
    api_key, base_url, default_model = normalize_openai_secret(cfg_raw)

    _hb = asyncio.create_task(_heartbeat(http, sup_url, task_id, worker_id))
    try:
        if kind == "node":
            result = await _run_node_task(
                http=http, llm_http=llm_http, sup_url=sup_url, ws_root=ws_root,
                registry=registry, task=item, worker_id=worker_id,
                session_id=session_id, session_generation=session_generation,
                api_key=api_key, base_url=base_url,
                default_model=default_model,
            )
        elif kind == "tool":
            result = await _run_tool_task(
                http=http, sup_url=sup_url, ws_root=ws_root,
                registry=registry, task=item, worker_id=worker_id,
                session_id=session_id, session_generation=session_generation,
                task_id=task_id,
            )
        else:
            result = {"action": "fail", "node_id": "", "error": f"未知 task kind: {kind}"}
    except asyncio.CancelledError:
        # Direction 1: engine graceful shutdown — write terminal state before exiting
        print(f"[engine] task {task_id} cancelled (engine shutdown)", flush=True)
        result = {"action": "cancelled", "node_id": str(item.get("node_id") or ""), "error": "engine graceful shutdown"}
    except Exception as exc:
        print(f"[engine] task {task_id} crashed: {exc}", flush=True)
        result = {"action": "fail", "node_id": str(item.get("node_id") or ""), "error": f"引擎内部错误: {exc}"}

    _hb.cancel()
    # 重试 complete_task POST，防止因网络/超时导致 task 永久孤立
    try:
        for _complete_attempt in range(3):
            try:
                _cr = await http.post(
                    f"{sup_url}/v1/tasks/{task_id}/complete",
                    json={"worker_id": worker_id, "result": result},
                )
                if _cr.status_code < 500:
                    break  # 成功或客户端错误（如 404），不再重试
            except asyncio.CancelledError:
                raise  # Propagate to outer handler for final attempt
            except Exception as _ce:
                print(f"[engine] task {task_id} complete POST failed (attempt {_complete_attempt + 1}/3): {_ce}", flush=True)
                if _complete_attempt < 2:
                    await asyncio.sleep(2 ** _complete_attempt)  # 1s, 2s
        else:
            print(f"[engine] task {task_id} ORPHANED - all complete POST attempts failed", flush=True)
    except asyncio.CancelledError:
        # Shutdown interrupted the retry loop; make one final attempt
        try:
            await http.post(
                f"{sup_url}/v1/tasks/{task_id}/complete",
                json={"worker_id": worker_id, "result": result},
            )
        except Exception:
            print(f"[engine] task {task_id} ORPHANED - shutdown interrupted complete POST", flush=True)


async def _run_node_task(
    *,
    http: httpx.AsyncClient,
    llm_http: httpx.AsyncClient,
    sup_url: str,
    ws_root: Path,
    registry: ToolRegistry,
    task: dict[str, Any],
    worker_id: str,
    session_id: str,
    session_generation: int,
    api_key: str,
    base_url: str,
    default_model: str,
) -> dict[str, Any]:
    task_id = str(task.get("task_id") or "").strip()
    node_id = str(task.get("node_id") or "").strip()
    source_inbound_seq = task.get("source_inbound_seq")
    input_data = task.get("input") if isinstance(task.get("input"), dict) else {}

    node = load_node(ws_root, node_id)
    if node is None:
        return {"action": "fail", "node_id": node_id, "error": f"节点未找到：{node_id}"}
    if not api_key:
        return {"action": "fail", "node_id": node_id, "error": "OpenAI api_key 未配置。"}

    rctx = RunContext(
        workspace_root=ws_root, supervisor_url=sup_url,
        session_id=session_id, worker_id=worker_id,
        http=http, llm_http=llm_http,
        api_key=api_key, base_url=base_url, default_model=default_model,
        user_text=str(input_data.get("instruction") or "").strip(),
        task_id=task_id, session_generation=session_generation,
        source_inbound_seq=int(source_inbound_seq) if source_inbound_seq is not None else None,
        task_context=input_data.get("task_context") if isinstance(input_data.get("task_context"), dict) else {},
    )
    # Phase 1 (Session Conversation Store): 创建 ConversationStore 实例并挂载到 rctx。
    # ai_step 中的 _shadow_write 通过 getattr(ls.rctx, 'conversation_store', None) 访问。
    # RunContext 是非 frozen dataclass，允许动态添加属性。
    # 数据目录: data/conversations/{session_id}.jsonl
    _conv_store = ConversationStore(ws_root / "data" / "conversations")
    rctx.conversation_store = _conv_store  # type: ignore[attr-defined]

    history = []
    resume_data_raw = input_data.get("resume_data") if isinstance(input_data.get("resume_data"), dict) else None
    is_resume = bool(resume_data_raw)

    # ====== Child Session 隔离（Phase B）：优先走 child session 路径 ======
    # child_session_id 由 supervisor 的 task_router 在 dispatch 时设置。
    # 有此字段时，子节点的历史从自己的 JSONL 文件加载，不再使用 snapshot。
    child_session_id = str(input_data.get("child_session_id") or "").strip()
    context_mode = str(input_data.get("context_mode") or "").strip()
    fork_from = str(input_data.get("fork_from_session_id") or "").strip()
    context_ref = str(input_data.get("context_ref") or "").strip()
    use_context = bool(input_data.get("use_context", True))

    # Step 2（2026-04-16）：主节点切 ConversationStore 的 feature flag。
    # flag 开启时，主节点（无 child_session_id）从 data/conversations/{session_id}.jsonl
    # 加载 history，与 child session 同源；_persist_ctx 不写 snapshot。
    # flag 关闭时回退到旧 snapshot 机制（context_ref + load_context_snapshot）。
    _runtime_cfg_main = load_runtime_config(ws_root)
    _main_conv_enabled = bool(
        _runtime_cfg_main.get("engine", {}).get("child_session", {}).get("main_session_enabled", True)
    )

    if child_session_id:
        # ---- Child Session 模式（新路径）----
        rctx.child_session_id = child_session_id

        if context_mode == "fork" and fork_from and not is_resume:
            # fork: 先从父 session 复制历史到子 session
            _conv_store.fork(fork_from, child_session_id)

        if not is_resume:
            # 从子 session 的 JSONL 加载历史，过滤 system 消息（子节点重建自己的 system prompt）
            stored = _conv_store.load(child_session_id)
            history = [_message_to_history_dict(m) for m in stored if m.role != "system"]
            history = _strip_trailing_pseudo_call(history)
        # child session 模式不使用 context_ref（让 ai_step 重建 system prompt）
        context_ref = ""
    elif _main_conv_enabled:
        # ---- 主节点 ConversationStore 模式（Step 2 新路径）----
        # 主节点入口 task 在 task_store.py 已不再注入 context_ref。
        # 此处从主 session 的 JSONL 加载 history，消息源和 child session 一致。
        # _shadow_write 从 Phase 1 起就已经把 user_input/assistant/tool_result
        # 写入了 data/conversations/{session_id}.jsonl，这里的 load 能拿到完整结构。
        # resume 场景（compact_done / child_result / child_preempted）也走这里：
        # 此时 context_ref 为空，ai_step 会走 assemble_initial_messages，需要 history 不为空。
        if use_context:
            stored = _conv_store.load(session_id)
            history = [_message_to_history_dict(m) for m in stored if m.role != "system"]
            # 剥离尾部伪工具（finish/dispatch 等），同 child session 路径处理
            history = _strip_trailing_pseudo_call(history)
            # 仅新 inbound（非 resume）需要 pop 尾部重复 instruction：
            # shadow_write 会在本轮再次写入 user 消息，若 JSONL 里上一条就是本轮 instruction，
            # 则要 pop 掉，避免 assemble_initial_messages 再次追加造成重复。
            if not is_resume:
                _cur_instr = str(input_data.get("instruction") or "").strip()
                if _cur_instr and history and history[-1].get("role") == "user":
                    _last_content = history[-1].get("content", "")
                    if isinstance(_last_content, str) and _last_content.strip() == _cur_instr:
                        history.pop()
        context_ref = ""  # 走 ai_step 的 assemble_initial_messages 重建系统提示
    elif context_ref and not is_resume:
        # ====== 旧路径（兼容期保留）：从 snapshot 加载 ======
        # 有上一轮 context_ref 但不是 resume：从快照提取非系统消息作为 enriched history，
        # 清空 context_ref 让 ai_step 重建新的系统提示词。
        snapshot = load_context_snapshot(ws_root, context_ref)
        if snapshot and isinstance(snapshot.get("messages"), list):
            history = [m for m in snapshot["messages"] if m.get("role") != "system"]
            # 剥离尾部的伪工具调用（finish/ask/dispatch_node），
            # 改为提取 finish/ask 的 text 作为正常 assistant 回复。
            history = _strip_trailing_pseudo_call(history)
        elif use_context:
            # 快照加载失败，降级为 session_messages
            history = await _fetch_history(rctx)
        context_ref = ""  # 让 ai_step 走 else 分支重建系统提示词
    elif not context_ref and use_context:
        history = await _fetch_history(rctx)

    ds_info = _collect_node_info(ws_root, list(node.delegate_targets))

    runtime_cfg = load_runtime_config(ws_root)
    sw_info = _discover_switchable_nodes(ws_root, node.id)
    entry_node_id = get_str(runtime_cfg, "shell.entry_node_id", "bootstrap.shell_orchestrator").strip()

    rp = resolve_provider(ws_root, node, default_model)
    provider = OpenAIProvider(
        http=llm_http,
        api_key=rp.api_key or api_key,
        base_url=rp.base_url or base_url or None,
        model=rp.model,
    )

    await rctx.emit_event("node_started", {
        "task_id": task_id, "node_id": node.id,
        "node_name": node.name,
    })

    input_attachments = input_data.get("attachments") if isinstance(input_data.get("attachments"), list) else None

    # 如果是被 switch 过来的节点，在 instruction 前注入提示
    instruction = str(input_data.get("instruction") or "").strip()
    switched_from = str(input_data.get("switched_from") or "").strip()
    if switched_from:
        instruction = f"[系统提示：你当前是通过节点切换接管此会话的。会话的默认入口节点是 {switched_from}。用户可以要求切回默认节点，你可以使用 switch_node 工具（target 传空字符串）恢复默认。]\n\n{instruction}"

    # Step 2（2026-04-16）：主节点 ConversationStore 模式下的 resume 去重保护。
    # resume 时（compact_done / child_result / child_preempted 等），JSONL 已包含原
    # instruction 及其后续全部对话；assemble_initial_messages 末尾再追加一次 instruction
    # 会破坏对话顺序并复发图片附件。旧 snapshot 路径下此 case 通过 snapshot.messages
    # 直接回放解决，新路径下需要在这里把 instruction 和 attachments 清空。
    # message_assembly.py 同步改为 instruction 为空且无 attachments 时不追加末尾消息。
    if _main_conv_enabled and not child_session_id and is_resume:
        instruction = ""
        input_attachments = None

    # Phase 1 补丁：将 user_input (instruction) 影子写入 ConversationStore。
    # 此前 _shadow_write 只写了 assistant 和 tool_result，用户指令完全缺失，
    # 导致子节点 accumulate 恢复时 JSONL 里缺少用户输入、上下文不完整。
    # 写入目标 session：优先 child_session_id（子节点隔离），否则 session_id（主节点）。
    #
    # Step 2（2026-04-16）：resume 场景（compact_done / child_result / child_failed 等）
    # 下 caller task 被 supervisor 重新唤醒，input.instruction 仍是原先的用户指令，
    # 此指令已在首次进入时写过 JSONL，这里不应再写一次。
    # 只在非 resume 场景追加 user_input 消息。
    if instruction and _conv_store and not is_resume:
        from datetime import datetime, timezone
        _target_sid = child_session_id or session_id
        _user_msg = Message(
            id=str(uuid.uuid4()),
            role="user",
            content=instruction,
            message_type=MessageType.USER_INPUT,
            created_at=datetime.now(timezone.utc).isoformat(),
            source_node_id=node.id,
            source_task_id=task_id,
        )
        _conv_store.append(_target_sid, _user_msg)

    # Phase 0/1: Signal System — 初始化信号总线并安装 JSONL 桥接。
    # get_bus() 返回全局单例，install_event_bridge 是幂等的（多次调用只注册一次）。
    # 读取 runtime.yaml 中的 signals.enabled 配置决定是否启用。
    _signals_cfg = runtime_cfg.get("engine", {}).get("signals", {})
    _sig_bus = get_bus()
    _sig_bus.enabled = bool(_signals_cfg.get("enabled", True))
    _bridge_patterns = _signals_cfg.get("bridge_patterns")
    install_event_bridge(_sig_bus, patterns=_bridge_patterns)

    action = await run_ai_node(
        rctx=rctx, provider=provider, registry=registry, node=node,
        instruction=instruction,
        history=history, run_id=task_id, context_ref=context_ref,
        resume_data=resume_data_raw,
        downstream_info=ds_info,
        switch_info=sw_info,
        streaming=bool(get_bool(runtime_cfg, "engine.streaming", False)),
        attachments=input_attachments,
    )

    # Child Session 隔离（Phase B）：将 child_session_id 写入 task result，
    # 供 supervisor 侧 _route_completed_task_locked 和调试/监控使用。
    if child_session_id:
        action.child_session_id = child_session_id

    await rctx.emit_event("node_completed", {
        "task_id": task_id, "node_id": node.id, "node_name": node.name,
        "action": action.action, "summary": action.summary,
        "source_inbound_seq": source_inbound_seq,
    })

    return action.to_dict()


async def _run_tool_task(
    *,
    http: httpx.AsyncClient,
    sup_url: str,
    ws_root: Path,
    registry: ToolRegistry,
    task: dict[str, Any],
    worker_id: str,
    session_id: str,
    session_generation: int,
    task_id: str,
) -> dict[str, Any]:
    input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
    tool_name = str(task.get("tool_name") or "").strip() or str(input_data.get("tool_name") or "").strip()
    arguments = dict(input_data.get("arguments") or {})

    kctx = ToolContext(
        supervisor_url=sup_url, session_id=session_id, run_id=task_id,
        worker_id=worker_id, workspace_root=ws_root, http=http,
        registry=registry, task_id=task_id,
        session_generation=session_generation, approval_poll_interval_sec=0.5,
    )

    await kctx.emit_event("handoff_progress", {
        "message": f"[tool] 开始执行 {tool_name}",
        "task_id": task_id, "tool_name": tool_name,
    })

    result = await registry.execute(name=tool_name, arguments=arguments, ctx=kctx)
    if isinstance(result, dict) and result.get("cancelled"):
        await kctx.emit_event("cancel_acknowledged", {"task_id": task_id, "tool_name": tool_name})
        return {
            "action": "cancelled",
            "node_id": tool_name,
            "summary": "任务已取消",
        }

    tool_attachments = result.get("attachments") if isinstance(result, dict) and isinstance(result.get("attachments"), list) else []

    summary = summarize_result(tool_name, result)
    fmt, raw = result_to_raw(tool_name, result)
    # [2026-04-17] 移除工具结果截断机制：不再对 raw 做 max_inline 截断和 artifact 写入，
    # 直接将完整结果传递给下游，避免信息丢失。
    raw_inline = raw

    await kctx.emit_event("handoff_progress", {
        "message": f"[tool] {tool_name}: {summary}",
        "task_id": task_id, "tool_name": tool_name,
    })

    return {
        "action": "finish",
        "node_id": tool_name,
        "summary": summary,
        "result": {
            "summary": summary,
            "text": raw_inline,
            "attachments": tool_attachments,
            "format": fmt,
            "truncated": False,  # [2026-04-17] 截断机制已移除，保留字段兼容下游
            "ref": "",
            # 旧字段保留供 tool_trace 格式化用
            "raw_format": fmt,
            "raw_inline": raw_inline,
            "tool_name": tool_name,
            "arguments": arguments,
        },
    }
