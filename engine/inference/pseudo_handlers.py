"""伪工具运行时处理。

从 ai_step.py 抽出。处理 7 种伪工具的执行逻辑。
"""
from __future__ import annotations

import asyncio
import json
import mimetypes as _mimetypes
from pathlib import Path
from typing import Any

from .resume_builder import _select_attachments
from ..compact import _format_messages_for_summary
from ..protocol import TaskAction, ACTION_DISPATCH, ACTION_FINISH
from .loop_state import _LoopState, _persist_ctx, _short
# 【Fix 3】reply 工具结果统一走 formatter.format_tool_result，需要 ParsedToolCall 构造、
# MessageMeta 标注 message_type、MessageType 影子写入。延迟导入 _shadow_write 避免循环依赖。
from .tool_format import ParsedToolCall
from .message_model import MessageMeta, set_message_meta
from ..conversation_store import MessageType


# ---------------------------------------------------------------------------
#  [2026-04-22] 辅助函数：将 workspace-relative 路径列表转换为 attachment dict 列表。
#  用于 dispatch_node / dispatch_nodes 伪工具，让父节点能将文件附件传给子节点。
#  仅检查文件是否存在并猜测 MIME 类型，不读取文件内容。
# ---------------------------------------------------------------------------
def _paths_to_attachments(paths: list, workspace_root: Path) -> list[dict]:
    """Convert workspace-relative file paths to attachment dicts for dispatch."""
    result = []
    for p in paths:
        p_str = str(p).strip()
        if not p_str:
            continue
        full = workspace_root / p_str
        if not full.exists():
            continue
        mime = _mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        att_type = "image" if mime.startswith("image/") else "file"
        result.append({
            "type": att_type,
            "path": p_str,
            "mime_type": mime,
            "name": full.name,
        })
    return result


async def _handle_pseudo_tool(ls: _LoopState, pseudo_call, step: int) -> TaskAction | None:
    """处理伪工具调用。

    返回 TaskAction 则退出循环（终止型伪工具或 compact dispatch）；
    返回 None 表示已处理完毕，调用方判断是否继续。
    """
    args = pseudo_call.arguments or {}

    # reply: 非终止，发送中间消息
    if pseudo_call.name == "reply":
        reply_text = str(args.get("text") or "").strip()
        if reply_text:
            await ls.rctx.emit_event("intermediate_reply", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "text": reply_text,
            })
            # 【Fix 3】内核统一通过 formatter.format_tool_result 落 tool_result，
            # 内容固定 "ok"——LLM 关心的是「reply 调用成功」这个事实，不需要回显原文。
            # 让 native / JSON / fake-native 各自的 formatter 吸收模式差异，内核不写 if-else。
            # 同时通过 set_message_meta 标 message_type=tool_result，并影子写入 ConversationStore，
            # 与真实工具结果走同一路径，避免后续遍历时被误判。
            from .ai_step import _shadow_write  # 延迟导入避免与 ai_step 之间的循环依赖
            _parsed = ParsedToolCall(
                id=getattr(pseudo_call, "id", "") or "",
                name="reply",
                arguments=dict(args),
            )
            tool_msg = ls.formatter.format_tool_result(_parsed, "ok")
            set_message_meta(tool_msg, MessageMeta(message_type="tool_result"))
            ls.messages.append(tool_msg)
            _shadow_write(ls, tool_msg, MessageType.TOOL_RESULT)
        return None

    # compact_context: 非终止，手动压缩
    if pseudo_call.name == "compact_context":
        return await _handle_pseudo_compact(ls, pseudo_call, step)

    # preempt_task: 非终止，软打断子任务
    if pseudo_call.name == "preempt_task":
        return await _handle_pseudo_preempt_task(ls, args)

    # dispatch_node: 非终止，异步委派
    if pseudo_call.name == "dispatch_node":
        return await _handle_pseudo_dispatch_node(ls, args)

    # dispatch_nodes: 非终止，批量异步委派
    if pseudo_call.name == "dispatch_nodes":
        return await _handle_pseudo_dispatch_nodes(ls, args)

    # ---- 终止型伪工具：finish / switch_node ----

    if pseudo_call.name == "finish":
        # [RFC 2026-04-20] finish 升级为真实 API 工具：在 _persist_ctx 之前构造
        # tool_result("completed") 存入历史，确保 Native 模式下 tool_use/tool_result
        # 配对完整。此内容不会被模型看到（循环即将终止），仅用于 API 配对校验
        # 和下一轮对话历史格式合法性。与 reply 伪工具的 tool_result 写入路径对齐。
        from .pseudo_tools import FINISH_TOOL_RESULT_CONTENT
        from .ai_step import _shadow_write  # 延迟导入避免循环依赖
        _finish_parsed = ParsedToolCall(
            id=getattr(pseudo_call, "id", "") or "",
            name="finish",
            arguments=dict(args),
        )
        _finish_result_msg = ls.formatter.format_tool_result(_finish_parsed, FINISH_TOOL_RESULT_CONTENT)
        set_message_meta(_finish_result_msg, MessageMeta(message_type="tool_result"))
        ls.messages.append(_finish_result_msg)
        _shadow_write(ls, _finish_result_msg, MessageType.TOOL_RESULT)

        ctx_ref = _persist_ctx(ls, step + 1)
        summary_text = str(args.get("summary") or "").strip()
        result_text = str(args.get("text") or "").strip()
        _selected_paths = args.get("attachment_paths")
        if isinstance(_selected_paths, list) and _selected_paths:
            final_atts = _select_attachments(
                ls.collected_attachments, _selected_paths,
                workspace_root=ls.rctx.workspace_root,
                session_id=ls.rctx.session_id,
            )
        else:
            final_atts = []
        return TaskAction(
            action=ACTION_FINISH, node_id=ls.node.id,
            result={
                "summary": summary_text,
                "text": result_text,
                "attachments": final_atts,
            },
            context_ref=ctx_ref,
            summary=_short(summary_text or result_text, 240),
        )

    # switch_node 也需要 ctx_ref，单独计算
    if pseudo_call.name == "switch_node":
        ctx_ref = _persist_ctx(ls, step + 1)
        switch_target = str(args.get("target") or "").strip()
        switch_text = str(args.get("text") or "").strip()
        try:
            await ls.rctx.http.post(
                f"{ls.rctx.supervisor_url}/v1/sessions/{ls.rctx.session_id}/switch_node",
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
                "attachments": list(ls.tool_produced_attachments),
            },
            context_ref=ctx_ref,
            summary=f"switch → {switch_target or 'default'}",
        )

    return None  # 未知伪工具，按非终止处理


async def _handle_pseudo_compact(ls: _LoopState, pseudo_call, step: int) -> TaskAction | None:
    """处理 compact_context 伪工具。可能返回 DISPATCH action。"""
    _manual_keep = ls.compact_keep_recent
    try:
        _kr_arg = pseudo_call.arguments.get("keep_recent")
        if _kr_arg is not None:
            _manual_keep = int(_kr_arg)
    except (TypeError, ValueError):
        pass
    try:
        await ls.rctx.emit_event("compact_start", {"node_id": ls.node.id, "step": step, "manual": True})
        conversation_text = _format_messages_for_summary(
            [m for m in ls.messages if m.get("role") != "system" and not m.get("_dynamic")]
        )
        if conversation_text.strip():
            ctx_ref = _persist_ctx(ls, step)
            return TaskAction(
                action=ACTION_DISPATCH,
                node_id=ls.node.id,
                target_node="system.compactor",
                context_ref=ctx_ref,
                dispatch_input={
                    "instruction": conversation_text,
                    "_compact_dispatch": True,
                    "context_mode": "fresh",
                    "_compact_keep_recent": _manual_keep,
                    "_system_task": True,
                    "use_context": False,
                },
            )
        else:
            ls.messages.append({
                "role": "user",
                "content": "[Context compression skipped: no compressible content.]",
            })
    except Exception as compact_err:
        await ls.rctx.emit_event("compact_failed", {"node_id": ls.node.id, "step": step, "error": str(compact_err)})
        ls.messages.append({
            "role": "user",
            "content": f"[Context compression failed: {compact_err}]",
        })
    return None


async def _handle_pseudo_preempt_task(ls: _LoopState, args: dict) -> None:
    """处理 preempt_task 伪工具。始终返回 None（非终止）。"""
    _pt_tid = str(args.get("task_id") or "").strip()
    if _pt_tid:
        try:
            _pt_resp = await ls.rctx.http.post(f"{ls.rctx.supervisor_url}/v1/tasks/{_pt_tid}/preempt")
            if _pt_resp.status_code == 200:
                _pt_result = f"[preempt_task result: 已标记 task {_pt_tid[:8]} 为 preempt，等待优雅退出]"
            elif _pt_resp.status_code == 404:
                _pt_result = f"[preempt_task result: task {_pt_tid[:8]} 不存在或已结束]"
            else:
                _pt_result = f"[preempt_task result: API 返回 {_pt_resp.status_code}]"
        except Exception as _pt_e:
            _pt_result = f"[preempt_task result: 调用失败 {_pt_e}]"
    else:
        _pt_result = "[preempt_task result: task_id 不能为空]"
    ls.messages.append({"role": "user", "content": _pt_result})
    return None


async def _handle_pseudo_dispatch_node(ls: _LoopState, args: dict) -> None:
    """处理 dispatch_node 伪工具。始终返回 None（非终止）。"""
    target = str(args.get("target") or "").strip()
    instr = str(args.get("instruction") or "").strip()
    ctx_mode = str(args.get("context_mode") or "accumulate").strip()
    ctx_key = str(args.get("context_key") or "").strip() or None
    # [2026-04-22] 从参数中提取 attachment_paths，转换为 attachment dicts 传给子节点
    attachment_paths = args.get("attachment_paths") or []
    attachments = _paths_to_attachments(attachment_paths, ls.rctx.workspace_root)
    try:
        payload: dict[str, Any] = {
            "session_id": ls.rctx.session_id,
            "session_generation": ls.rctx.session_generation,
            "node_id": target,
            "instruction": instr,
            "context_mode": ctx_mode,
            "context_key": ctx_key,
            "caller_node_id": ls.node.id,
        }
        # [2026-04-22] 仅在有附件时附加，避免无谓的空列表
        if attachments:
            payload["attachments"] = attachments
        _dispatch_resp = await ls.rctx.http.post(
            f"{ls.rctx.supervisor_url}/v1/tasks/dispatch-async",
            json=payload,
            timeout=10.0,
        )
        if _dispatch_resp.status_code == 200:
            _d_data = _dispatch_resp.json()
            _dispatch_result = json.dumps({"success": True, "task_id": _d_data.get("task_id", ""), "message": f"已异步委派给 {target}"}, ensure_ascii=False)
        else:
            _dispatch_result = json.dumps({"success": False, "error": f"dispatch API 返回 {_dispatch_resp.status_code}: {_dispatch_resp.text}"}, ensure_ascii=False)
    except Exception as e:
        _dispatch_result = json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
    ls.messages.append({
        "role": "user",
        "content": f"[dispatch_node result: {_dispatch_result}]",
    })
    return None


async def _do_one_dispatch(
    ls: _LoopState, _t_target: str, _t_instr: str, _t_ctx_mode: str, _t_ctx_key: str | None,
    attachments: list | None = None,  # [2026-04-22] 新增：传递附件给子节点
) -> dict:
    """执行单个异步委派请求，供 dispatch_nodes 批量调用。"""
    try:
        payload: dict[str, Any] = {
            "session_id": ls.rctx.session_id,
            "session_generation": ls.rctx.session_generation,
            "node_id": _t_target,
            "instruction": _t_instr,
            "context_mode": _t_ctx_mode,
            "context_key": _t_ctx_key,
            "caller_node_id": ls.node.id,
        }
        # [2026-04-22] 仅在有附件时附加
        if attachments:
            payload["attachments"] = attachments
        _dispatch_resp = await ls.rctx.http.post(
            f"{ls.rctx.supervisor_url}/v1/tasks/dispatch-async",
            json=payload,
            timeout=10.0,
        )
        if _dispatch_resp.status_code == 200:
            _d_data = _dispatch_resp.json()
            return {"target": _t_target, "success": True, "task_id": _d_data.get("task_id", "")}
        else:
            return {"target": _t_target, "success": False, "error": f"HTTP {_dispatch_resp.status_code}"}
    except Exception as e:
        return {"target": _t_target, "success": False, "error": str(e)}


async def _handle_pseudo_dispatch_nodes(ls: _LoopState, args: dict) -> None:
    """处理 dispatch_nodes 伪工具（批量异步委派）。始终返回 None（非终止）。"""
    tasks_list = args.get("tasks")
    if not isinstance(tasks_list, list) or not tasks_list:
        _dispatch_result = json.dumps({"success": False, "error": "tasks 列表为空或格式错误"}, ensure_ascii=False)
    else:
        # [2026-04-22] 从每个 task_item 中提取 attachment_paths 并转换，传给 _do_one_dispatch
        _coros = [
            _do_one_dispatch(
                ls,
                str(_task_item.get("target") or "").strip(),
                str(_task_item.get("instruction") or "").strip(),
                str(_task_item.get("context_mode") or "accumulate").strip(),
                str(_task_item.get("context_key") or "").strip() or None,
                attachments=_paths_to_attachments(
                    _task_item.get("attachment_paths") or [], ls.rctx.workspace_root,
                ) or None,
            )
            for _task_item in tasks_list
        ]
        _batch_results = list(await asyncio.gather(*_coros, return_exceptions=True))
        for i, r in enumerate(_batch_results):
            if isinstance(r, BaseException):
                _batch_results[i] = {"target": str(tasks_list[i].get("target", "?")), "success": False, "error": str(r)}
        _dispatch_result = json.dumps({"success": True, "tasks": _batch_results}, ensure_ascii=False)
    ls.messages.append({
        "role": "user",
        "content": f"[dispatch_nodes result: {_dispatch_result}]",
    })
    return None
