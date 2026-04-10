from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx

from clonoth_runtime import (
    fetch_openai_secret,
    get_bool,
    get_float,
    get_str,
    load_runtime_config,
    normalize_openai_secret,
)
from providers.openai import OpenAIProvider
from toolbox.context import ToolContext
from toolbox.registry import ToolRegistry

from .ai_step import run_ai_node
from .context import RunContext
from .model import resolve_provider
from .context_store import load_context_snapshot

from .node import Node, load_node
from .tool_step import result_to_raw, summarize_result, write_artifact


def _collect_downstream_info(workspace_root: Path, node: Node) -> list[dict[str, str]]:
    """收集下游节点基本信息（id/name/description），用于 dispatch_node 工具描述。"""
    result: list[dict[str, str]] = []
    for target_id in (node.delegate_targets or []):
        target = load_node(workspace_root, target_id)
        if target is None:
            continue
        result.append({
            "id": target.id,
            "name": target.name,
            "description": target.description or target.name,
        })
    return result


_PSEUDO_TOOL_NAMES = {"finish", "ask", "dispatch_node", "dispatch_nodes", "reply"}


def _strip_trailing_pseudo_call(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip trailing pseudo-tool call from the last assistant message in history.

    When restoring history from a context snapshot, the last assistant message
    may contain a pseudo-tool record like '[Tool call history record: finish was executed with args: {...}]'.
    For finish: extract the text param and replace with a normal assistant reply.
    For ask/dispatch_node/dispatch_nodes: drop or trim.
    """
    if not history:
        return history

    last = history[-1]
    if last.get("role") != "assistant":
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
        # Extract the text param from '[Tool call history record: finish was executed with args: {"text": "..."}]'
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
                        result[-1] = {"role": "assistant", "content": combined}
                        return result
                except Exception:
                    pass

    # ask/dispatch_node or parse failure: keep pre_text or drop the message
    if pre_text:
        result[-1] = {"role": "assistant", "content": pre_text}
        return result
    return result[:-1]


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
                        result.append({"role": str(m.get("role")), "content": content})
                    else:
                        result.append({"role": str(m.get("role")), "content": str(content)})
                return result
    except Exception:
        pass
    return []


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
    runtime_cfg = load_runtime_config(workspace_root)
    registry = ToolRegistry(workspace_root=workspace_root, tools_dir=workspace_root / "tools")
    _last_reload_seq = 0

    sup_timeout = get_float(runtime_cfg, "engine.http.client_timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    llm_timeout = get_float(runtime_cfg, "providers.openai.timeout_sec", 60.0, min_value=5.0, max_value=600.0)
    poll_sec = get_float(runtime_cfg, "engine.poll_interval_sec", 1.0, min_value=0.1, max_value=60.0)

    async with (
        httpx.AsyncClient(timeout=sup_timeout, trust_env=False) as http,
        httpx.AsyncClient(timeout=llm_timeout, trust_env=False) as llm_http,
    ):
        await wait_supervisor(http, supervisor_url)
        mcp_count = await registry.load_mcp_tools()
        if mcp_count:
            print(f"[engine] loaded {mcp_count} MCP tools", flush=True)

        while True:
            try:
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

                nr = await http.get(f"{supervisor_url}/v1/tasks/next", params={"worker_id": wid})
                if nr.status_code == 200:
                    item = nr.json()
                    if isinstance(item, dict) and item.get("task_id"):
                        await _handle_task(http, llm_http, supervisor_url, workspace_root, registry, item, wid)
                        continue
            except Exception as e:
                print(f"[engine] error: {e}", flush=True)
            await asyncio.sleep(poll_sec)


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

    await http.post(
        f"{sup_url}/v1/tasks/{task_id}/complete",
        json={"worker_id": worker_id, "result": result},
    )


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
    )

    history = []
    context_ref = str(input_data.get("context_ref") or "").strip()
    use_context = bool(input_data.get("use_context", True))
    resume_data_raw = input_data.get("resume_data") if isinstance(input_data.get("resume_data"), dict) else None
    is_resume = bool(resume_data_raw)

    if context_ref and not is_resume:
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

    ds_info = _collect_downstream_info(ws_root, node)

    runtime_cfg = load_runtime_config(ws_root)
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

    action = await run_ai_node(
        rctx=rctx, provider=provider, registry=registry, node=node,
        instruction=str(input_data.get("instruction") or "").strip(),
        history=history, run_id=task_id, context_ref=context_ref,
        resume_data=resume_data_raw,
        downstream_info=ds_info,
        streaming=bool(get_bool(runtime_cfg, "engine.streaming", False)),
        attachments=input_attachments,
    )

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
    max_inline = 8000
    truncated = len(raw) > max_inline
    ref = ""
    if truncated:
        ref = await write_artifact(ws_root, task_id, str(input_data.get("call_id") or task_id), tool_name, fmt, raw)
    raw_inline = raw if not truncated else raw[:max_inline] + "\n...<truncated>"

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
            "truncated": truncated,
            "ref": ref,
            # 旧字段保留供 tool_trace 格式化用
            "raw_format": fmt,
            "raw_inline": raw_inline,
            "tool_name": tool_name,
            "arguments": arguments,
        },
    }
