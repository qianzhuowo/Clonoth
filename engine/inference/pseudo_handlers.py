"""伪工具运行时处理。

从 ai_step.py 抽出。处理静态伪工具、dispatch_to 与向后兼容别名伪工具的执行逻辑。
"""
from __future__ import annotations

import copy
import json
import mimetypes as _mimetypes
import re as _re
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .resume_builder import _select_attachments
from ..compact_flow import prepare_compaction
from ..protocol import TaskAction, ACTION_ASK, ACTION_DISPATCH, ACTION_FINISH
from .loop_state import _LoopState, _short
from .tool_format import ParsedToolCall
from .pseudo_tools import _dispatch_target_from_tool_name
from .message_model import MessageMeta, set_message_meta
from ..conversation_store import MessageType


def _persist_ctx(ls: _LoopState, step: int) -> str:
    from .ai_step import _persist_ctx as persist
    return persist(ls, step)


def _paths_to_attachments(paths: list, workspace_root: Path) -> list[dict]:
    """Convert file paths to attachment dicts for dispatch."""
    result = []
    for p in paths:
        p_str = str(p).strip()
        if not p_str:
            continue
        full = Path(p_str) if Path(p_str).is_absolute() else workspace_root / p_str
        full = full.resolve()
        if not full.exists():
            continue
        mime = _mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        att_type = "image" if mime.startswith("image/") else "file"
        result.append({
            "type": att_type,
            "path": str(full),
            "mime_type": mime,
            "name": full.name,
        })
    return result


def _emit_pseudo_tool_result(
    ls: _LoopState,
    pseudo_call,
    content: str,
    *,
    persist: bool = True,
    control_tool_name: str = "",
    control_tool_status: str = "",
    meta_patch: dict[str, Any] | None = None,
) -> None:
    """统一写入伪工具的 tool_result，确保 native 模式下 tool_use/tool_result 配对完整。"""
    from .ai_step import _shadow_write
    _parsed = ParsedToolCall(
        id=getattr(pseudo_call, "id", "") or "",
        name=pseudo_call.name,
        arguments=dict(pseudo_call.arguments or {}),
    )
    tool_msg = ls.formatter.format_tool_result(_parsed, content)
    if control_tool_name:
        tool_msg["_ephemeral"] = True
        if _parsed.id:
            tool_msg.setdefault("tool_call_id", _parsed.id)
        tool_msg.setdefault("name", control_tool_name)
    set_message_meta(tool_msg, MessageMeta(
        tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
        message_type="tool_result",
        control_tool_name=control_tool_name,
        control_tool_status=control_tool_status,
    ))
    if meta_patch:
        tool_msg.setdefault("_meta", {}).update(meta_patch)
    ls.messages.append(tool_msg)
    if persist:
        _shadow_write(ls, tool_msg, MessageType.TOOL_RESULT)


async def _handle_pseudo_tool(ls: _LoopState, pseudo_call, step: int) -> TaskAction | None:
    """处理伪工具调用。

    返回 TaskAction 则退出循环（终止型伪工具或 compact dispatch）；
    返回 None 表示已处理完毕，调用方判断是否继续。
    """
    args = pseudo_call.arguments or {}

    # intermediate_reply: 非终止，发送中间消息
    if pseudo_call.name == "intermediate_reply":
        reply_text = str(args.get("text") or "").strip()
        _reply_att_paths = args.get("attachment_paths") or args.get("attachments") or []
        _reply_atts = _paths_to_attachments(_reply_att_paths, ls.rctx.workspace_root) if _reply_att_paths else []
        if reply_text or _reply_atts:
            _reply_payload: dict[str, Any] = {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "text": reply_text,
            }
            if _reply_atts:
                _reply_payload["attachments"] = _reply_atts
            await ls.rctx.emit_event("intermediate_reply", _reply_payload)
        _emit_pseudo_tool_result(ls, pseudo_call, "ok")
        return None

    # compact_context: 非终止，手动压缩
    if pseudo_call.name == "compact_context":
        return await _handle_pseudo_compact(ls, pseudo_call, step)

    # manage_workers (with legacy aliases preempt_task and cancel_active_tasks)
    if pseudo_call.name == "manage_workers":
        return await _handle_manage_workers(ls, args, pseudo_call)
    if pseudo_call.name == "preempt_task":
        return await _handle_pseudo_preempt_task(ls, pseudo_call, args)
    if pseudo_call.name == "cancel_active_tasks":
        _cat_args = dict(args)
        _cat_args["action"] = "cancel"
        return await _handle_manage_workers(ls, _cat_args, pseudo_call)

    # spawn_worker: non-terminal anonymous delegation
    if pseudo_call.name == "spawn_worker":
        return await _handle_spawn_worker(ls, args, pseudo_call)

    # dispatch_to: unified dynamic delegation tool
    if pseudo_call.name == "dispatch_to":
        return await _handle_pseudo_dispatch(ls, args, pseudo_call)

    # Legacy dispatch_to_{target_id} compatibility
    _fixed_dispatch_target = _dispatch_target_from_tool_name(pseudo_call.name)
    if _fixed_dispatch_target:
        return await _handle_pseudo_dispatch(ls, {**args, "target": _fixed_dispatch_target}, pseudo_call)

    # ---- 终止型伪工具：finish / ask / switch_node ----
    if pseudo_call.name in ("finish", "ask"):
        terminal_name = str(pseudo_call.name or "").strip()
        result_text = str(args.get("text") or "").strip()
        _call_id = getattr(pseudo_call, "id", "") or ""
        _call_args = dict(pseudo_call.arguments or {})
        if not result_text:
            _reject_text = (
                f'❌ REJECTED: {terminal_name}() called with empty text. Your visible content MUST go '
                f'in the {terminal_name} tool\'s `text` parameter, NOT in free prose outside tool calls. '
                'Free prose is never delivered to the user. Put your actual answer/question/data '
                f'in text and call {terminal_name} again.'
            )
            _emit_pseudo_tool_result(ls, pseudo_call, _reject_text)
            await ls.rctx.emit_event("tool_call_end", {
                "tool_call_id": _call_id,
                "tool_name": terminal_name,
                "status": "error",
                "rejected": True,
                "rejection_code": "empty_text",
                "error": f"{terminal_name}() called with empty text",
                "raw_inline": _reject_text,
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
            })
            return None

        ls.rctx.tool_call_log.append({
            "name": terminal_name,
            "call_id": _call_id,
            "args_summary": str(_call_args)[:200],
        })

        ctx_ref = _persist_ctx(ls, step + 1)
        final_atts = []
        if terminal_name == "finish":
            _selected_paths = args.get("attachment_paths") or args.get("attachments")
            if isinstance(_selected_paths, list) and _selected_paths:
                final_atts = _select_attachments(
                    ls.collected_attachments, _selected_paths,
                    workspace_root=ls.rctx.workspace_root,
                    session_id=ls.rctx.session_id,
                )

        _tool_start_payload: dict[str, Any] = {
            "tool_call_id": _call_id,
            "tool_name": terminal_name,
            "arguments": _call_args,
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
        }
        if final_atts:
            _tool_start_payload["attachments"] = list(final_atts)
        await ls.rctx.emit_event("tool_call_start", _tool_start_payload)
        _tool_meta_patch: dict[str, Any] = {
            "llm_request_id": getattr(ls.rctx, "current_llm_request_id", ""),
            "tool_result_raw_inline": "ok",
            "tool_result_format": "text",
        }
        if final_atts:
            _tool_meta_patch["attachments"] = list(final_atts)
            _tool_meta_patch["tool_result_attachments"] = list(final_atts)
        _emit_pseudo_tool_result(ls, pseudo_call, "ok", meta_patch=_tool_meta_patch)

        result_payload = {
            "text": result_text,
        }
        if terminal_name == "finish":
            result_payload["attachments"] = final_atts
        return TaskAction(
            action=ACTION_ASK if terminal_name == "ask" else ACTION_FINISH,
            node_id=ls.node.id,
            result=result_payload,
            context_ref=ctx_ref,
            llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
        )

    # switch_node 也需要 ctx_ref，单独计算
    if pseudo_call.name == "switch_node":
        switch_atts = list(ls.tool_produced_attachments)
        _emit_pseudo_tool_result(ls, pseudo_call, "ok", meta_patch={
            "llm_request_id": getattr(ls.rctx, "current_llm_request_id", ""),
            "attachments": switch_atts,
            "tool_result_attachments": switch_atts,
            "tool_result_raw_inline": "ok",
            "tool_result_format": "text",
        })
        ctx_ref = _persist_ctx(ls, step + 1)
        switch_target = str(args.get("target") or "").strip()
        switch_text = str(args.get("text") or "").strip()
        route_session_id = getattr(ls.rctx, "parent_session_id", "") or ls.rctx.session_id
        try:
            await ls.rctx.http.post(
                f"{ls.rctx.supervisor_url}/v1/sessions/{route_session_id}/switch_node",
                json={"target_node_id": switch_target},
            )
        except Exception:
            pass
        await ls.rctx.emit_event("node_switch", {
            "target_node_id": switch_target,
            "node_id": ls.node.id,
        })
        return TaskAction(
            action=ACTION_FINISH, node_id=ls.node.id,
            result={
                "text": switch_text,
                "attachments": switch_atts,
            },
            context_ref=ctx_ref,
            llm_request_id=getattr(ls.rctx, "current_llm_request_id", ""),
        )

    return None  # 未知伪工具，按非终止处理


async def _handle_pseudo_compact(ls: _LoopState, pseudo_call, step: int) -> TaskAction | None:
    """处理 compact_context 伪工具。可能返回 DISPATCH action。"""
    _manual_keep = ls.compact_keep_recent
    try:
        _kr_arg = pseudo_call.arguments.get("keep_recent")
        if _kr_arg is not None:
            _manual_keep = int(_kr_arg)
    except (AttributeError, TypeError, ValueError):
        pass

    target_session_id = str(
        getattr(ls.rctx, "child_session_id", "")
        or getattr(ls.rctx, "parent_session_id", "")
        or getattr(ls.rctx, "session_id", "")
        or ""
    ).strip()

    try:
        await ls.rctx.emit_event("compact_start", {"node_id": ls.node.id, "step": step, "manual": True})
        plan = await prepare_compaction(
            messages=ls.messages,
            target_session_id=target_session_id,
            node_id=ls.node.id,
            keep_recent=_manual_keep,
        )
        if plan.reason.startswith("truncated:"):
            try:
                original_chars = int(plan.reason.split(":", 1)[1])
            except (IndexError, ValueError):
                original_chars = 0
            await ls.rctx.emit_event("ptl_truncated", {
                "node_id": ls.node.id,
                "step": step,
                "original_chars": original_chars,
            })
        if plan.status == "dispatch":
            _emit_pseudo_tool_result(ls, pseudo_call, "compacting...")
            ctx_ref = _persist_ctx(ls, step)
            return TaskAction(
                action=ACTION_DISPATCH,
                node_id=ls.node.id,
                target_node="system.compactor",
                context_ref=ctx_ref,
                dispatch_input=plan.dispatch_input,
            )
        if plan.status == "skip":
            _emit_pseudo_tool_result(ls, pseudo_call, f"skipped: {plan.reason or 'no compressible content'}")
        else:
            await ls.rctx.emit_event("compact_failed", {
                "node_id": ls.node.id,
                "step": step,
                "error": plan.reason,
            })
            _emit_pseudo_tool_result(ls, pseudo_call, f"failed: {plan.reason}")
    except Exception as compact_err:
        await ls.rctx.emit_event("compact_failed", {"node_id": ls.node.id, "step": step, "error": str(compact_err)})
        _emit_pseudo_tool_result(ls, pseudo_call, f"failed: {compact_err}")
    return None


_UUID_FULL_PATTERN = _re.compile(r'^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$', _re.I)


def _looks_like_task_id(s: str) -> bool:
    """Return True if s looks like a UUID or hex prefix (for task_id prefix match)."""
    if _UUID_FULL_PATTERN.fullmatch(s):
        return True
    return len(s) >= 8 and all(c in '0123456789abcdef-' for c in s.lower())


async def _handle_pseudo_preempt_task(ls: _LoopState, pseudo_call, args: dict) -> None:
    """处理旧 preempt_task 别名调用。始终返回 None（非终止）。"""
    _pt_tid = str(args.get("task_id") or args.get("target") or args.get("node_id") or "").strip()
    _pt_msg = str(args.get("message") or "").strip()
    if _pt_tid:
        try:
            real_task_id = _pt_tid
            if not _looks_like_task_id(_pt_tid):
                _route_sid = ls.rctx.parent_session_id or ls.rctx.session_id
                _node_resp = await ls.rctx.http.get(
                    f"{ls.rctx.supervisor_url}/v1/sessions/{_route_sid}/tasks/by-node/{_pt_tid}",
                )
                if _node_resp.status_code == 200:
                    _node_data = _node_resp.json()
                    real_task_id = _node_data.get("task_id", "")
                elif _node_resp.status_code == 404:
                    _global_resp = await ls.rctx.http.get(
                        f"{ls.rctx.supervisor_url}/v1/tasks/active-by-node/{_pt_tid}",
                    )
                    if _global_resp.status_code == 200:
                        _global_data = _global_resp.json()
                        real_task_id = _global_data.get("task_id", "")
                    else:
                        _pt_result = f"节点 '{_pt_tid}' 在当前 session 及全局范围内均无活跃任务"
                        _emit_pseudo_tool_result(ls, pseudo_call, _pt_result)
                        return None
                else:
                    _pt_result = f"查询节点 '{_pt_tid}' 的任务失败: HTTP {_node_resp.status_code}"
                    _emit_pseudo_tool_result(ls, pseudo_call, _pt_result)
                    return None

            _pt_body: dict[str, Any] = {}
            if _pt_msg:
                _pt_body["message"] = _pt_msg
            _pt_resp = await ls.rctx.http.post(
                f"{ls.rctx.supervisor_url}/v1/tasks/{real_task_id}/preempt",
                json=_pt_body if _pt_body else None,
            )
            _display_id = _pt_tid if _looks_like_task_id(_pt_tid) else f"{_pt_tid}({real_task_id[:8]})"
            if _pt_resp.status_code == 200:
                if _pt_msg:
                    _pt_result = f"已向 task {_display_id} 注入追加指令，子任务继续执行"
                else:
                    _pt_result = f"已标记 task {_display_id} 为 preempt，等待优雅退出"
            elif _pt_resp.status_code == 404:
                _pt_result = f"task {_display_id} 不存在或已结束"
            else:
                _pt_result = f"API 返回 {_pt_resp.status_code}"
        except Exception as _pt_e:
            _pt_result = f"调用失败 {_pt_e}"
    else:
        _pt_result = "task_id 不能为空"
    _emit_pseudo_tool_result(ls, pseudo_call, _pt_result)
    return None


async def _handle_spawn_worker(ls: _LoopState, args: dict, pseudo_call) -> None:
    """Create an anonymous ephemeral sub-agent through shared dispatch handling."""
    instr = str(args.get("instruction") or "").strip()
    if not instr:
        _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
            "success": False,
            "error": "instruction is required",
        }, ensure_ascii=False))
        return None

    ctx_mode = str(args.get("context_mode") or "fork").strip()
    if ctx_mode not in ("fresh", "fork"):
        ctx_mode = "fork"

    template = str(args.get("template") or "_worker").strip() or "_worker"
    raw_attachment_paths = args.get("attachment_paths") or []
    attachment_paths = raw_attachment_paths if isinstance(raw_attachment_paths, list) else []

    dispatch_args = {
        "target": template,
        "instruction": instr,
        "context_mode": ctx_mode,
        "attachment_paths": attachment_paths,
    }
    return await _handle_pseudo_dispatch(ls, dispatch_args, pseudo_call)


def _resolve_active_child_task_id(tasks: list[dict[str, Any]], requested: str) -> tuple[str, str]:
    """Resolve a full/unique-prefix task id OR node_id within the active child set."""
    needle = str(requested or "").strip()
    if not needle:
        return "", "task_id is required"
    matches = [
        str(task.get("task_id") or "").strip()
        for task in tasks
        if isinstance(task, dict) and str(task.get("task_id") or "").strip().startswith(needle)
    ]
    matches = list(dict.fromkeys(task_id for task_id in matches if task_id))
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return "", f"ambiguous task_id prefix: {needle}"
    # Try matching by node_id inside the active child task set
    node_matches = [
        str(task.get("task_id") or "").strip()
        for task in tasks
        if isinstance(task, dict) and str(task.get("node_id") or "").strip() == needle
    ]
    node_matches = list(dict.fromkeys(task_id for task_id in node_matches if task_id))
    if len(node_matches) == 1:
        return node_matches[0], ""
    if len(node_matches) > 1:
        return node_matches[-1], ""
    return "", f"active child task not found: {needle}"


async def _resolve_child_target_with_fallback(
    ls: _LoopState,
    tasks: list[dict[str, Any]],
    requested: str,
) -> tuple[str, str]:
    """Resolve target via active child list first, then fallback to supervisor by-node lookup."""
    needle = str(requested or "").strip()
    if not needle:
        return "", "target is required"
    task_id, err = _resolve_active_child_task_id(tasks, needle)
    if task_id or err.startswith("ambiguous"):
        return task_id, err
    # Fallback to supervisor by-node endpoints when needle looks like a node_id
    if not _looks_like_task_id(needle) and hasattr(ls, "rctx") and hasattr(ls.rctx, "http"):
        get_fn = getattr(ls.rctx.http, "get", None)
        if callable(get_fn):
            route_sid = getattr(ls.rctx, "parent_session_id", "") or getattr(ls.rctx, "session_id", "")
            node_resp = await get_fn(f"{ls.rctx.supervisor_url}/v1/sessions/{route_sid}/tasks/by-node/{needle}")
            if node_resp.status_code == 200:
                tid = str((node_resp.json() or {}).get("task_id") or "").strip()
                if tid:
                    return tid, ""
            elif node_resp.status_code == 404:
                glob_resp = await get_fn(f"{ls.rctx.supervisor_url}/v1/tasks/active-by-node/{needle}")
                if glob_resp.status_code == 200:
                    tid = str((glob_resp.json() or {}).get("task_id") or "").strip()
                    if tid:
                        return tid, ""
    return "", err


def _task_elapsed_seconds(created_at: Any) -> int | None:
    """Return elapsed whole seconds for an ISO timestamp, if parseable."""
    raw = str(created_at or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return None


async def _handle_manage_workers(ls: _LoopState, args: dict, pseudo_call) -> None:
    """List, message, gracefully preempt, or cancel active child agents in the routed session."""
    action = str(args.get("action") or "").strip()
    route_session_id = str(
        getattr(ls.rctx, "parent_session_id", "")
        or getattr(ls.rctx, "session_id", "")
        or ""
    ).strip()

    async def _active_tasks() -> list[dict[str, Any]]:
        discover = getattr(ls.rctx, "list_active_child_tasks", None)
        if not callable(discover):
            return []
        result = await discover()
        return [dict(item) for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    if action == "list":
        try:
            tasks = await _active_tasks()
            task_list: list[dict[str, Any]] = []
            for task in tasks:
                created_at = str(task.get("created_at") or "")
                task_list.append({
                    "task_id": str(task.get("task_id") or "")[:12],
                    "node_id": str(task.get("node_id") or ""),
                    "status": str(task.get("status") or ""),
                    "created_at": created_at,
                    "elapsed_sec": _task_elapsed_seconds(created_at),
                })
            result = json.dumps({
                "active_children": len(task_list),
                "tasks": task_list,
            }, ensure_ascii=False)
        except Exception as exc:
            result = json.dumps({"error": str(exc)}, ensure_ascii=False)
        _emit_pseudo_tool_result(ls, pseudo_call, result)
        return None

    if action in ("message", "preempt"):
        requested_target = str(args.get("target") or args.get("task_id") or args.get("node_id") or "").strip()
        message = str(args.get("message") or "").strip()
        if action == "message" and (not requested_target or not message):
            _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
                "success": False,
                "error": "task_id and message are required for 'message' action",
            }, ensure_ascii=False))
            return None
        if action == "preempt" and not requested_target:
            _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
                "success": False,
                "error": "target (or task_id/node_id) is required for 'preempt' action",
            }, ensure_ascii=False))
            return None
        try:
            task_id, resolution_error = await _resolve_child_target_with_fallback(ls, await _active_tasks(), requested_target)
            if resolution_error:
                result = json.dumps({"success": False, "error": resolution_error}, ensure_ascii=False)
            else:
                body = {"message": message} if message else None
                post_kwargs: dict[str, Any] = {"timeout": 10.0}
                if body is not None:
                    post_kwargs["json"] = body
                response = await ls.rctx.http.post(
                    f"{ls.rctx.supervisor_url}/v1/tasks/{task_id}/preempt",
                    **post_kwargs,
                )
                if response.status_code == 200:
                    result = json.dumps({
                        "success": True,
                        "message": f"Instruction injected to {task_id[:12]}" if message else f"Graceful preempt requested for {task_id[:12]}",
                    }, ensure_ascii=False)
                else:
                    result = json.dumps({
                        "success": False,
                        "error": f"API returned {response.status_code}: {response.text}",
                    }, ensure_ascii=False)
        except Exception as exc:
            result = json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
        _emit_pseudo_tool_result(ls, pseudo_call, result)
        return None

    if action == "cancel":
        requested_target = str(args.get("target") or args.get("task_id") or "").strip()
        explicit_node_id = str(args.get("node_id") or "").strip()
        try:
            if explicit_node_id and not requested_target:
                params = {
                    "exclude_task_id": str(getattr(ls.rctx, "task_id", "") or ""),
                    "node_id": explicit_node_id,
                }
                response = await ls.rctx.http.post(
                    f"{ls.rctx.supervisor_url}/v1/sessions/{route_session_id}/cancel_active_tasks",
                    params=params,
                    timeout=10.0,
                )
                cancelled = f"node:{explicit_node_id}"
            elif requested_target:
                tasks = await _active_tasks()
                task_id, resolution_error = await _resolve_child_target_with_fallback(ls, tasks, requested_target)
                if resolution_error:
                    # If target is a node_id and no active tasks matched locally, allow session-level cancel by node_id
                    if not _looks_like_task_id(requested_target) and not tasks:
                        params = {
                            "exclude_task_id": str(getattr(ls.rctx, "task_id", "") or ""),
                            "node_id": requested_target,
                        }
                        response = await ls.rctx.http.post(
                            f"{ls.rctx.supervisor_url}/v1/sessions/{route_session_id}/cancel_active_tasks",
                            params=params,
                            timeout=10.0,
                        )
                        cancelled = f"node:{requested_target}"
                    else:
                        _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
                            "success": False,
                            "error": resolution_error,
                        }, ensure_ascii=False))
                        return None
                else:
                    response = await ls.rctx.http.post(
                        f"{ls.rctx.supervisor_url}/v1/tasks/{task_id}/cancel",
                        timeout=10.0,
                    )
                    cancelled = task_id[:12]
            else:
                response = await ls.rctx.http.post(
                    f"{ls.rctx.supervisor_url}/v1/sessions/{route_session_id}/cancel_active_tasks",
                    params={"exclude_task_id": str(getattr(ls.rctx, "task_id", "") or "")},
                    timeout=10.0,
                )
                cancelled = "all"
            if response.status_code == 200:
                result = json.dumps({"success": True, "cancelled": cancelled}, ensure_ascii=False)
            else:
                result = json.dumps({
                    "success": False,
                    "error": f"API returned {response.status_code}: {response.text}",
                }, ensure_ascii=False)
        except Exception as exc:
            result = json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
        _emit_pseudo_tool_result(ls, pseudo_call, result)
        return None

    _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
        "success": False,
        "error": f"Unknown action: {action}. Use 'list', 'message', 'preempt', or 'cancel'.",
    }, ensure_ascii=False))
    return None


def _extract_cache_warm_fork_prefix(ls: _LoopState) -> list[dict[str, Any]]:
    """Extract the exact prefix of ls.messages that was sent to the LLM in the current step.

    Strips any trailing tool results emitted earlier in the current step and the
    current step's assistant tool_call message, plus any _ephemeral messages.
    """
    raw_msgs = ls.messages
    if not raw_msgs:
        return []
    end = len(raw_msgs)
    while end > 0 and raw_msgs[end - 1].get("role") == "tool":
        end -= 1
    if end > 0 and raw_msgs[end - 1].get("role") == "assistant" and raw_msgs[end - 1].get("tool_calls"):
        end -= 1
    prefix = [copy.deepcopy(m) for m in raw_msgs[:end] if not m.get("_ephemeral")]
    from .llm_call import _build_messages_for_provider
    return _build_messages_for_provider(prefix, None, None)


async def _handle_pseudo_dispatch(ls: _LoopState, args: dict, pseudo_call) -> None:
    """处理 dispatch_to / spawn_worker / dispatch_to_{target_id} 动态伪工具。始终返回 None（非终止）。"""
    target = str(args.get("target") or "").strip()
    if not target:
        _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
            "success": False,
            "error": "target is required",
        }, ensure_ascii=False))
        return None

    if target == "_worker":
        from engine.builtin.agent_manage import ensure_worker_node_yaml
        ensure_worker_node_yaml(ls.rctx.workspace_root)

    instr = str(args.get("instruction") or "").strip()
    if not instr:
        _emit_pseudo_tool_result(ls, pseudo_call, json.dumps({
            "success": False,
            "error": "instruction is required",
        }, ensure_ascii=False))
        return None

    _raw_ctx_mode = args.get("context_mode")
    if _raw_ctx_mode is not None:
        ctx_mode = str(_raw_ctx_mode).strip()
    else:
        ctx_mode = "accumulate"
    ctx_key = str(args.get("context_key") or "").strip() or None
    attachment_paths = args.get("attachment_paths") or []
    attachments = _paths_to_attachments(attachment_paths, ls.rctx.workspace_root)

    is_ephemeral_worker = (pseudo_call.name == "spawn_worker" or target == "_worker")

    # Only default persistent nodes to accumulate when NOT an ephemeral worker (spawn_worker)
    # and when the caller did not explicitly request fresh/fork (or when persistent node defaults apply).
    if not is_ephemeral_worker and ctx_mode != "accumulate" and _raw_ctx_mode is None:
        from ..node import load_node as _load_node
        _target_node = _load_node(Path(ls.rctx.workspace_root), target)
        if _target_node is not None and getattr(_target_node, "persistent", False):
            ctx_mode = "accumulate"

    parent_session_id = getattr(ls.rctx, "parent_session_id", "") or ls.rctx.session_id
    task_ctx = getattr(ls.rctx, "task_context", None) or {}
    parent_conv_key = str(
        task_ctx.get("route_conversation_key")
        or task_ctx.get("conversation_key")
        or ""
    ).strip()
    parent_channel = task_ctx.get("channel", "internal")
    target_node_id = target

    if ctx_mode in ("fresh", "fork") or is_ephemeral_worker:
        conv_key = f"agent:{target_node_id}:{parent_conv_key}:{_uuid.uuid4()}"
    else:  # accumulate
        if ctx_key:
            conv_key = f"agent:{target_node_id}:{ctx_key}:{parent_conv_key}"
        else:
            conv_key = f"agent:{target_node_id}:{parent_conv_key}"
        _seen_conv = getattr(ls, "_step_dispatch_conv_keys", None)
        if _seen_conv is None:
            _seen_conv = {}
            setattr(ls, "_step_dispatch_conv_keys", _seen_conv)
        _step_tag = f"{getattr(ls.rctx, 'current_llm_request_id', '')}:{conv_key}"
        _cnt = _seen_conv.get(_step_tag, 0) + 1
        _seen_conv[_step_tag] = _cnt
        if _cnt > 1:
            conv_key = f"{conv_key}:par{_cnt}"

    dispatch_origin: dict[str, Any] = {
        "parent_session_id": parent_session_id,
        "caller_node_id": ls.node.id,
        "origin_task_id": str(getattr(ls.rctx, "task_id", "") or ""),
        "parent_conversation_key": parent_conv_key,
        "context_mode": ctx_mode,
    }

    # Cache-Warm Fork: when spawning a fork worker (or fork dispatch), persist the parent's
    # exact cache-warmed prefix (system prompt + static blocks + history + tools + sticky_session_id)
    # so the child worker hits 100% of the parent session's upstream KV cache!
    if ctx_mode == "fork" and is_ephemeral_worker and target == "_worker":
        parent_sticky_sid = str(getattr(ls.rctx, "sticky_session_id", "") or parent_session_id or "").strip()
        if parent_sticky_sid:
            dispatch_origin["parent_sticky_session_id"] = parent_sticky_sid
        prefix_msgs = _extract_cache_warm_fork_prefix(ls)
        if prefix_msgs:
            snap_dir = Path(ls.rctx.workspace_root) / "data" / "temp" / "fork_snapshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            snap_name = f"fork_{_uuid.uuid4().hex}.json"
            snap_path = snap_dir / snap_name
            snap_payload = {
                "messages": prefix_msgs,
                "openai_tools": copy.deepcopy(getattr(ls, "openai_tools", None) or []),
                "sticky_session_id": parent_sticky_sid,
            }
            snap_path.write_text(json.dumps(snap_payload, ensure_ascii=False), encoding="utf-8")
            dispatch_origin["fork_snapshot_path"] = f"data/temp/fork_snapshots/{snap_name}"

    inbound_payload: dict[str, Any] = {
        "channel": parent_channel,
        "conversation_key": conv_key,
        "text": instr,
        "entry_node_id": target_node_id,
        "use_context": True,
        "attachments": attachments or [],
        "dispatch_origin": dispatch_origin,
        "dispatch_context_mode": ctx_mode,
    }
    # Dispatch children act on behalf of the originating platform user, so
    # supervisor permission checks (QQ admin / non-admin) still apply to them.
    _parent_auth = task_ctx.get("platform_auth")
    if isinstance(_parent_auth, dict) and _parent_auth:
        inbound_payload["platform_auth"] = dict(_parent_auth)
    if ctx_mode == "fork":
        inbound_payload["dispatch_fork_from_session"] = getattr(ls.rctx, "session_id", "") or parent_session_id

    _call_id = getattr(pseudo_call, "id", "") or ""
    _call_args = dict(pseudo_call.arguments or {})
    await ls.rctx.emit_event("tool_call_start", {
        "tool_call_id": _call_id,
        "tool_name": pseudo_call.name,
        "arguments": _call_args,
    })

    try:
        _dispatch_resp = await ls.rctx.http.post(
            f"{ls.rctx.supervisor_url}/v1/inbound",
            json=inbound_payload,
            timeout=10.0,
        )
        if _dispatch_resp.status_code == 200:
            _d_data = _dispatch_resp.json()
            _sid_short = str(_d_data.get("session_id", ""))[:8]
            _dispatch_result = json.dumps({
                "success": True,
                "session_id": _d_data.get("session_id", ""),
                "message": (
                    f"⏳ Task delegated to {target} (child session: {_sid_short}). "
                    "The result will arrive wrapped in <dispatch_result>, delivered via preempt "
                    "while this task is running, or as a follow-up message after it ends. "
                    "You can finish this task or continue working on other things."
                ),
            }, ensure_ascii=False)
        else:
            _dispatch_result = json.dumps({
                "success": False,
                "error": f"inbound API returned {_dispatch_resp.status_code}: {_dispatch_resp.text}",
            }, ensure_ascii=False)
    except Exception as e:
        _dispatch_result = json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    await ls.rctx.emit_event("tool_call_end", {
        "tool_call_id": _call_id,
        "tool_name": pseudo_call.name,
        "status": "success" if '"success": true' in _dispatch_result else "error",
        "result": _dispatch_result,
    })
    _emit_pseudo_tool_result(ls, pseudo_call, _dispatch_result)
    return None
