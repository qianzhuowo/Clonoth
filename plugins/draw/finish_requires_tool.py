"""finish 硬校验：节点声明 ``finish_requires_tool`` 时，finish 前必须真有工具成功。

Why: 绘图/媒体节点的模型会在自然语言里编造工具结果（“生图成功”），从未真正
调用工具就 finish，导致谎报成功、图片从未生成也未发送。
How:
- ``after_tool_call``：把本任务内成功的同步真实工具记入 ``ls.plugin_data``；
  引擎核心没有成功工具登记，插件私有状态空间正是为此准备的。
- ``terminal_tool``：finish（ask 不受限）执行前核对。若没有任何所需工具成功，
  再扫描消息历史中的异步占位 ``Async tool "<name>" started``（异步工具不触发
  after_tool_call，且结果回传会 fork 新 task、新 loop_state 为空——扫描历史
  就是修复“Rejected finish 死循环”的关键）。仍不满足则写入配对的拒绝
  tool_result、拦截本次 finish，任务继续让模型真正去调用工具。

节点 YAML 声明（未知字段落入 Node.extra）::

    finish_requires_tool:
      any: [nai_generate_from_plan, nai_generate]
"""
from __future__ import annotations

import logging
from typing import Any

from engine.hooks.types import HookResult

logger = logging.getLogger(__name__)

STATE_KEY = "draw.finish_requires_tool"

REJECTION_CODE = "finish_requires_tool"

REQUIRE_TOOL_MESSAGE = (
    "\u274c REJECTED: finish() cannot claim success yet. This node requires a "
    "successful call to one of these tools before finishing: {tools}. "
    "You have NOT actually called it successfully in this task \u2014 do not fabricate "
    "a tool result or claim the image/media was generated or sent. Call the real "
    "tool now, wait for its actual result, and only finish after it truly succeeds. "
    "If the tool keeps failing, finish with an honest failure message instead."
)


def required_tools(node: Any) -> set[str]:
    """读取 node.extra['finish_requires_tool']，支持 {any: [...]} / 列表 / 字符串。"""
    extra = getattr(node, "extra", None) if node is not None else None
    if not isinstance(extra, dict):
        return set()
    raw = extra.get("finish_requires_tool")
    if isinstance(raw, dict):
        names = raw.get("any") or raw.get("tools") or []
    elif isinstance(raw, (list, tuple)):
        names = list(raw)
    elif isinstance(raw, str):
        names = [raw]
    else:
        names = []
    return {str(n).strip() for n in names if str(n).strip()}


def _is_success(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("cancelled") or result.get("error"):
            return False
        if result.get("ok") is False:
            return False
    return True


def _succeeded(ls: Any) -> set[str]:
    data = getattr(ls, "plugin_data", None)
    if not isinstance(data, dict):
        return set()
    return set((data.get(STATE_KEY) or {}).get("succeeded") or ())


def _message_text(msg: Any) -> str:
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return ""


def history_has_async_started(messages: Any, required: set[str]) -> bool:
    """消息历史里是否有所需工具的异步启动占位（engine/builtin/async_scheduler.py）。"""
    if not isinstance(messages, list):
        return False
    markers = [f'Async tool "{name}" started' for name in required]
    for msg in messages:
        text = _message_text(msg)
        if text and any(marker in text for marker in markers):
            return True
    return False


class FinishSuccessRecorder:
    """after_tool_call：记录本任务内成功的所需工具。"""

    name = "draw.finish_requires_tool.record"
    priority = 100

    async def handle(self, ctx: Any) -> None:
        required = required_tools(getattr(ctx, "node", None))
        tool_name = str(ctx.extra.get("tool_name") or "")
        if not required or tool_name not in required:
            return None
        ls = ctx.extra.get("loop_state")
        if ls is None or not _is_success(ctx.extra.get("tool_result")):
            return None
        state = ls.plugin_data.setdefault(STATE_KEY, {})
        state.setdefault("succeeded", set()).add(tool_name)
        return None


class FinishRequiresToolGate:
    """terminal_tool：未满足前置的 finish 被拦截（先于 preempt 拦截运行）。"""

    name = "draw.finish_requires_tool"
    # 高于 preempt(100)：本 handler 拦截时 stop_chain，保证一个 finish 只配一条结果；
    # preempt 会在下一轮 before_step 照常检查。
    priority = 110

    async def handle(self, ctx: Any) -> HookResult | None:
        call = ctx.extra.get("terminal_call") or getattr(ctx, "tool_call", None)
        if str(getattr(call, "name", "") or "") != "finish":
            return None
        required = required_tools(getattr(ctx, "node", None))
        if not required:
            return None
        ls = ctx.extra.get("loop_state")
        if ls is None:
            return None
        succeeded = _succeeded(ls)
        if succeeded & required:
            return None
        if history_has_async_started(getattr(ctx, "messages", None), required):
            return None

        node_id = str(getattr(getattr(ctx, "node", None), "id", "") or "")
        logger.warning(
            "Rejected finish without required tool success (node=%s, step=%s, required=%s, succeeded=%s)",
            node_id, getattr(ctx, "step", -1), sorted(required), sorted(succeeded),
        )
        reject_text = REQUIRE_TOOL_MESSAGE.format(tools=", ".join(sorted(required)))
        # 与引擎 finish 空文本拒绝走同一条写回路径：配对 tool_result + 落库。
        from engine.inference.pseudo_handlers import _emit_pseudo_tool_result

        _emit_pseudo_tool_result(ls, call, reject_text, meta_patch={
            "tool_rejected": True,
            "tool_rejection_code": REJECTION_CODE,
        })
        await ls.rctx.emit_event("tool_call_end", {
            "tool_call_id": getattr(call, "id", "") or "",
            "tool_name": "finish",
            "status": "error",
            "rejected": True,
            "rejection_code": REJECTION_CODE,
            "error": "finish() requires a successful tool call first",
            "raw_inline": reject_text,
            "node_id": node_id,
            "task_id": getattr(ls.rctx, "task_id", ""),
        })
        ctx.stop_chain()
        return HookResult(channels={"intercepted": True})
