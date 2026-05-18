"""伪工具运行时处理。

从 ai_step.py 抽出。处理静态伪工具和 dispatch:{target_id} 动态伪工具的执行逻辑。
"""
from __future__ import annotations

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
from .pseudo_tools import _dispatch_target_from_tool_name
from .message_model import MessageMeta, set_message_meta
from ..conversation_store import MessageType


# ---------------------------------------------------------------------------
#  [2026-04-22] 辅助函数：将 workspace-relative 路径列表转换为 attachment dict 列表。
#  [2026-05-04] 现在只服务 dispatch:{target_id} 动态伪工具。
#  为什么：旧聚合委派工具已删除，但动态委派仍需把父节点文件传给子节点。
#  怎么做：保留路径到 attachment dict 的转换函数，删除旧分支调用。
#  目的：让动态 dispatch 的附件行为保持不变。
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


def _emit_pseudo_tool_result(
    ls: _LoopState,
    pseudo_call,
    content: str,
    *,
    persist: bool = True,
    control_tool_name: str = "",
    control_tool_status: str = "",
) -> None:
    """统一写入伪工具的 tool_result，确保 native 模式下 tool_use/tool_result 配对完整。

    [2026-05-07] 正常 finish 也通过本函数写普通工具结果。
    原因：finish 现在是完整落盘的真实 API 工具，不能再被改写为普通 assistant 文本。
    做法：默认 persist=True 且不设置 control 标记；只有未交付的拦截类结果才使用 control 参数。
    目的：保留 provider 配对，同时让长期历史保存 assistant.tool_call + tool_result。
    """
    from .ai_step import _shadow_write
    _parsed = ParsedToolCall(
        id=getattr(pseudo_call, "id", "") or "",
        name=pseudo_call.name,
        arguments=dict(pseudo_call.arguments or {}),
    )
    tool_msg = ls.formatter.format_tool_result(_parsed, content)
    if control_tool_name:
        # [2026-05-07] 控制流工具结果必须保留运行期配对字段，但不能成为长期历史。
        # 原因：finish 是真实 provider tool_use，需要 ACK；同时 fake-native/json 结果默认没有 call_id，旧清洗只能按全局兜底处理。
        # 做法：给控制 ACK 标记 _ephemeral，并补齐 tool_call_id/name，供当轮内存配对和清洗函数精确识别。
        # 目的：满足 provider 配对要求，同时避免 finish 结果进入 ConversationStore、快照、压缩和摘要。
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
    ls.messages.append(tool_msg)
    if persist:
        _shadow_write(ls, tool_msg, MessageType.TOOL_RESULT)


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
            _emit_pseudo_tool_result(ls, pseudo_call, "ok")
        return None

    # compact_context: 非终止，手动压缩
    if pseudo_call.name == "compact_context":
        return await _handle_pseudo_compact(ls, pseudo_call, step)

    # preempt_task: 非终止，软打断子任务
    if pseudo_call.name == "preempt_task":
        return await _handle_pseudo_preempt_task(ls, pseudo_call, args)

    # [2026-05-04] dispatch:{target_id}: 非终止，固定目标异步委派。
    # Why: dynamic per-target tools remove the target parameter from the schema.
    # How: extract target_id from the tool name and pass it to the shared dispatch
    # sender. Purpose: keep supervisor API behavior identical while making target
    # selection happen at tool registration time.
    _fixed_dispatch_target = _dispatch_target_from_tool_name(pseudo_call.name)
    if _fixed_dispatch_target:
        return await _handle_pseudo_dispatch(ls, {**args, "target": _fixed_dispatch_target}, pseudo_call)

    # ---- 终止型伪工具：finish / switch_node ----

    if pseudo_call.name == "finish":
        # [2026-05-07] 正常 finish 重新按真实 API 工具处理。
        # 原因：provider 原生工具协议要求 assistant.tool_call 后面保留普通 tool_result，
        # 若把 finish 改写成普通 assistant 文本，会破坏下一轮历史配对。
        # 做法：与 reply、真实业务工具一样写入 content="ok" 的 tool_result，并允许影子持久化。
        # 目的：ConversationStore、snapshot、provider replay 都能看到完整 finish 工具轮。
        _emit_pseudo_tool_result(ls, pseudo_call, "ok")

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
        _emit_pseudo_tool_result(ls, pseudo_call, "ok")
        ctx_ref = _persist_ctx(ls, step + 1)
        switch_target = str(args.get("target") or "").strip()
        switch_text = str(args.get("text") or "").strip()
        # [Fork/Merge 2026-05-17] Why: switch_node changes the entry node for
        # future inbound messages, which are keyed by the parent conversation
        # session, not the temporary branch runtime session. How: prefer
        # rctx.parent_session_id for the supervisor endpoint. Purpose: node
        # switching remains effective after the current branch is merged/cleaned.
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
            # Dispatch 路径：写 tool_result 后退出循环
            _emit_pseudo_tool_result(ls, pseudo_call, "compacting...")
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
            _emit_pseudo_tool_result(ls, pseudo_call, "skipped: no compressible content")
    except Exception as compact_err:
        await ls.rctx.emit_event("compact_failed", {"node_id": ls.node.id, "step": step, "error": str(compact_err)})
        _emit_pseudo_tool_result(ls, pseudo_call, f"failed: {compact_err}")
    return None


async def _handle_pseudo_preempt_task(ls: _LoopState, pseudo_call, args: dict) -> None:
    """处理 preempt_task 伪工具。始终返回 None（非终止）。"""
    _pt_tid = str(args.get("task_id") or "").strip()
    if _pt_tid:
        try:
            _pt_resp = await ls.rctx.http.post(f"{ls.rctx.supervisor_url}/v1/tasks/{_pt_tid}/preempt")
            if _pt_resp.status_code == 200:
                _pt_result = f"已标记 task {_pt_tid[:8]} 为 preempt，等待优雅退出"
            elif _pt_resp.status_code == 404:
                _pt_result = f"task {_pt_tid[:8]} 不存在或已结束"
            else:
                _pt_result = f"API 返回 {_pt_resp.status_code}"
        except Exception as _pt_e:
            _pt_result = f"调用失败 {_pt_e}"
    else:
        _pt_result = "task_id 不能为空"
    _emit_pseudo_tool_result(ls, pseudo_call, _pt_result)
    return None


async def _handle_pseudo_dispatch(ls: _LoopState, args: dict, pseudo_call) -> None:
    """处理 dispatch:{target_id} 动态伪工具。始终返回 None（非终止）。"""
    target = str(args.get("target") or "").strip()
    instr = str(args.get("instruction") or "").strip()
    ctx_mode = str(args.get("context_mode") or "accumulate").strip()
    ctx_key = str(args.get("context_key") or "").strip() or None
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
        # [Fork/Merge 2026-05-17] Why: async dispatch API may receive a branch
        # session from older engine paths or after a supervisor index rebuild.
        # How: include the parent route session when RunContext has it. Purpose:
        # supervisor can anchor async child tasks to the durable conversation.
        parent_session_id = getattr(ls.rctx, "parent_session_id", "") or ""
        if parent_session_id:
            payload["parent_session_id"] = parent_session_id
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
    _emit_pseudo_tool_result(ls, pseudo_call, _dispatch_result)
    return None
