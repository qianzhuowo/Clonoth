"""绘图 agent 入口：before_prompt_build 钩子接管 draw.novelai_planner。

流程：装配材料（已录入角色 / 绘图请求）→ 按布局组装规划消息链 →
PlannerRuntime 跑模型直到拿到合法的 submit_scene_plan → 逐张编译 NovelAI 请求并生成 →
每张完成即经 Supervisor 直发给用户（async_tool_result 快通道）→ 返回 finish。
引擎常规循环不再参与：provider 只由运行时调用，历史由这里影子写入 ConversationStore。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from engine.conversation_store import Message, MessageType
from engine.hooks import HookResult
from engine.protocol import ACTION_FINISH, TaskAction

from ..backends import novelai
from ..character_inject import _build_scan_text, _matches, _norm
from ..planner.assembly import build_planner_messages
from ..planner.contract import (
    SUBMIT_SCENE_PLAN_TOOL_NAME,
    ScenePlannerError,
    build_tool_protocol_retry_correction,
    parse_submitted_scene_plan,
)
from ..planner.frame import (
    build_character_info, build_content_block, build_frame_text, build_guide_section,
    build_limits_line, build_rules_section,
)
from ..planner.tool import create_submit_scene_plan_tool
from ..presets import effective_max_characters
from . import llm
from .config import PlannerConfig, load_planner_config
from .protocol import create_tool_protocol
from .runtime import PlannerRequest, PlannerResult, PlannerRuntime

logger = logging.getLogger(__name__)

TARGET_NODE_ID = "draw.novelai_planner"
HOOK_NAME = "draw.agent_entry"
# 高于 character_inject(40) / 其他注入器：接管后返回 finish，后续钩子与引擎循环都不再执行。
HOOK_PRIORITY = 900
MAX_PRESENT_CHARACTERS = 8
SEND_TOOL_NAME = "nai_generate"
# 重试前的等待；测试里替换成不等待的版本。
_sleep = asyncio.sleep


def _finish(node_id: str, text: str, attachments: list[dict[str, Any]] | None = None) -> HookResult:
    result: dict[str, Any] = {"text": text}
    if attachments:
        result["attachments"] = attachments
    return HookResult(action=TaskAction(action=ACTION_FINISH, node_id=node_id, result=result))


def _text_of(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type", "text") == "text")
    return str(content or "")


def match_present_characters(characters: list[dict[str, Any]], instruction: str, history: Any) -> list[dict[str, Any]]:
    scan = _norm(_build_scan_text(instruction, history))
    if not scan:
        return []
    matched = [c for c in characters if _matches(scan, c)]
    return matched[:MAX_PRESENT_CHARACTERS]


def registry_tool_to_openai(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": str(spec.get("name") or ""), "description": str(spec.get("description") or ""),
        "parameters": spec.get("input_schema") or {"type": "object", "properties": {}},
    }}


def _api_tool_calls(calls: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for call in calls or []:
        if not isinstance(call, Mapping):
            continue
        args = call.get("arguments")
        out.append({"id": str(call.get("id") or ""), "type": "function", "function": {
            "name": str(call.get("name") or ""),
            "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False),
        }})
    return out


class DrawAgentEntry:
    """before_prompt_build：draw.novelai_planner 的独立规划与生图 agent。"""

    name = HOOK_NAME
    priority = HOOK_PRIORITY

    def __init__(self, registry: Any = None) -> None:
        self.registry = registry

    # ---- history ----

    def _shadow_write(self, rctx: Any, node_id: str, message: Mapping[str, Any], message_type: str) -> None:
        store = getattr(rctx, "conversation_store", None)
        if store is None:
            return
        try:
            msg = Message(
                id=str(uuid4()), role=str(message.get("role") or "user"),
                content=message.get("content") if isinstance(message.get("content"), (str, list)) else str(message.get("content") or ""),
                message_type=message_type, created_at=datetime.now(timezone.utc).isoformat(),
                meta={"draw_agent": True},
                response_info=dict(getattr(rctx, "response_info", {}) or {}) if message.get("role") == "assistant" else {},
                source_node_id=node_id, source_task_id=str(getattr(rctx, "task_id", "") or ""),
                tool_calls=_api_tool_calls(message.get("tool_calls")),
                tool_call_id=str(message.get("tool_call_id") or ""), name=str(message.get("name") or ""),
            )
            store.append(getattr(rctx, "child_session_id", "") or rctx.session_id, msg)
            if not getattr(rctx, "first_shadow_message_id", ""):
                rctx.first_shadow_message_id = msg.id
            rctx.last_shadow_message_id = msg.id
        except Exception:  # noqa: BLE001
            logger.debug("draw agent: 影子写入失败", exc_info=True)

    def _on_message(self, rctx: Any, node_id: str, message: dict[str, Any]) -> None:
        role = message.get("role")
        if role == "assistant":
            self._shadow_write(rctx, node_id, message, MessageType.ASSISTANT)
        elif role == "tool":
            self._shadow_write(rctx, node_id, message, MessageType.TOOL_RESULT)
        elif role == "user":
            self._shadow_write(rctx, node_id, message, MessageType.USER_INPUT)

    # ---- tools ----

    def _custom_tool_specs(self, node: Any) -> list[dict[str, Any]]:
        if self.registry is None:
            return []
        access = getattr(node, "tool_access", None)
        mode = str(getattr(access, "mode", "none") or "none")
        if mode == "allowlist":
            names = [str(n) for n in (getattr(access, "allow", None) or [])]
        elif mode == "all":
            names = [str(s.get("name") or "") for s in self.registry.list_specs()]
        else:
            names = []
        specs: list[dict[str, Any]] = []
        for name in names:
            if name == SUBMIT_SCENE_PLAN_TOOL_NAME:
                continue
            spec = self.registry.get_spec(name)
            if spec:
                specs.append(spec)
        return specs

    def _tool_executor(self, rctx: Any, node_id: str):
        registry = self.registry

        async def execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if registry is None:
                return {"ok": False, "error": "tool registry unavailable"}
            from toolbox.context import ToolContext
            tool_ctx = ToolContext(
                supervisor_url=rctx.supervisor_url, session_id=rctx.session_id, run_id=rctx.task_id,
                worker_id=rctx.worker_id, workspace_root=rctx.workspace_root, http=rctx.http, registry=registry,
                task_id=rctx.task_id, session_generation=rctx.session_generation,
                parent_session_id=rctx.parent_session_id, node_id=node_id,
            )
            return await registry.execute(name=name, arguments=arguments, ctx=tool_ctx)

        return execute

    # ---- delivery ----

    @staticmethod
    def _delivery_session_id(rctx: Any) -> str:
        """图片直发的目标会话。

        被其他节点委派（dispatch_to）时，本节点跑在子会话里，子会话的会话键是
        ``agent:<节点>:<父键>…``，频道适配器无法由它找到真实投递目标，图片会变成死信。
        这时改发到发起委派的父会话；直达入口（/生图）没有 dispatch_origin，仍用当前会话。
        """
        task_context = getattr(rctx, "task_context", None)
        origin = task_context.get("dispatch_origin") if isinstance(task_context, Mapping) else None
        if isinstance(origin, Mapping):
            parent = str(origin.get("parent_session_id") or "").strip()
            if parent:
                return parent
        return str(getattr(rctx, "session_id", "") or "")

    async def _send_image(self, rctx: Any, node_id: str, path: str) -> bool:
        http = getattr(rctx, "http", None)
        if http is None or not getattr(rctx, "supervisor_url", ""):
            return False
        session_id = self._delivery_session_id(rctx)
        if not session_id:
            return False
        try:
            resp = await http.post(
                f"{rctx.supervisor_url}/v1/sessions/{session_id}/async_tool_result",
                json={"message": "", "attachment_paths": [path], "task_id": rctx.task_id,
                      "node_id": node_id, "tool_name": SEND_TOOL_NAME, "success": True},
            )
            status = int(getattr(resp, "status_code", 200) or 200)
            return 200 <= status < 300
        except Exception as exc:  # noqa: BLE001
            logger.warning("draw agent: 直发图片失败 path=%s: %s", path, exc)
            return False

    # ---- main ----

    async def handle(self, ctx: Any) -> HookResult | None:
        node = getattr(ctx, "node", None)
        node_id = str(getattr(node, "id", "") or "")
        if node_id != TARGET_NODE_ID:
            return None
        rctx = ctx.rctx
        workspace_root = Path(getattr(rctx, "workspace_root", "") or ".")
        extra = ctx.extra or {}
        history = extra.get("history") or []
        instruction = str(extra.get("instruction_text") or "").strip()
        if not instruction:
            for msg in reversed(history):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    instruction = _text_of(msg.get("content")).strip()
                    break
        if not instruction:
            text = "" if extra.get("is_resume") else "没有收到绘图请求，请告诉我要画什么。"
            return _finish(node_id, text)

        cfg = load_planner_config(workspace_root)
        try:
            result = await self._plan_with_retry(ctx, cfg, node_id, instruction, history)
        except ScenePlannerError as error:
            retries = int((error.details or {}).get("retries") or 0)
            text = f"场景规划失败：{error.message}" + (f"（已自动重试 {retries} 次）" if retries else "")
            self._shadow_write(rctx, node_id, {"role": "assistant", "content": text}, MessageType.ASSISTANT)
            return _finish(node_id, text)

        text, attachments = await self._generate(rctx, cfg, node_id, result)
        self._shadow_write(rctx, node_id, {"role": "assistant", "content": text}, MessageType.ASSISTANT)
        return _finish(node_id, text, attachments)

    def _planner_provider(self, ctx: Any, cfg: PlannerConfig) -> Any:
        """规划用的模型调用对象：场景 Agent 预设选了渠道方案就按方案创建，否则用节点自身的。"""
        agent_preset = cfg.agent_preset
        if not llm.preset_is_configured(agent_preset):
            return ctx.provider
        rctx = ctx.rctx
        http = getattr(rctx, "llm_http", None) or getattr(rctx, "http", None)
        try:
            provider = llm.create_provider(llm.resolve_preset(agent_preset, rctx.workspace_root), http=http)
            provider.channel_id = agent_preset["channel"]
            return provider
        except Exception as error:  # noqa: BLE001
            raise ScenePlannerError(
                f"场景 Agent 预设「{agent_preset.get('name')}」无法使用：{error}", "AGENT_PRESET_INVALID") from error

    async def _plan_with_retry(self, ctx: Any, cfg: PlannerConfig, node_id: str, instruction: str,
                               history: Any) -> PlannerResult:
        """整次规划失败后的自动重试（只重试规划，不重试出图）。单次规划内的校验纠错由 PlannerRuntime 负责。"""
        rctx = ctx.rctx
        max_retries = llm.normalize_planner_max_retries(cfg.agent.get("planner_max_retries"))
        provider = self._planner_provider(ctx, cfg)
        retries = 0
        correction = ""
        while True:
            try:
                return await self._plan(ctx, cfg, node_id, instruction, history, provider, correction)
            except ScenePlannerError as error:
                if retries >= max_retries or not llm.is_retryable_planner_error(error):
                    if retries:
                        error.details = {**(error.details or {}), "retries": retries}
                    raise
                retries += 1
                # 上一次是工具调用格式的问题时，下一次在原始消息后附一条纠错（只针对上一次，不累积）。
                correction = build_tool_protocol_retry_correction(error)
                delay = llm.planner_retry_delay(retries)
                logger.warning("draw agent: 规划失败（%s：%s），%.1f 秒后进行第 %d/%d 次重试",
                               error.code, error.message, delay, retries, max_retries)
                await _sleep(delay)
                check_cancelled = getattr(rctx, "check_cancelled", None)
                if check_cancelled is not None and await check_cancelled():
                    raise ScenePlannerError("场景规划已取消。", "REQUEST_ABORTED") from error

    async def _plan(self, ctx: Any, cfg: PlannerConfig, node_id: str, instruction: str, history: Any,
                    provider: Any, retry_correction: str = "") -> PlannerResult:
        rctx = ctx.rctx
        planner = cfg.planner
        preset = cfg.preset
        agent_preset = cfg.agent_preset
        present = match_present_characters(cfg.characters, instruction, history)
        limits = {
            "max_images": preset.get("max_images", 0),
            "max_plan_images": planner.get("max_plan_images", 0),
            # V5 有硬上限 10，预设填 0 时就是 10；其它模型按预设。
            "max_characters_per_image": effective_max_characters(preset),
        }
        texts = {
            "opening": cfg.texts.get("opening", ""),
            "frame": build_frame_text(cfg.profile),
            "content": build_content_block(
                planner.get("content_template"), character_info=build_character_info(present),
                draw_request=instruction,
            ),
            "guide": build_guide_section(cfg.texts.get("guide", ""), cfg.profile),
            "rules": build_rules_section(cfg.texts.get("rules", "")),
            "limits": build_limits_line(limits["max_images"], limits["max_characters_per_image"], limits["max_plan_images"]),
        }
        submit_tool = create_submit_scene_plan_tool(
            profile=cfg.profile, descriptions=planner.get("field_descriptions") or None, **limits,
        )
        custom_specs = self._custom_tool_specs(ctx.node)
        tools = [submit_tool, *(registry_tool_to_openai(s) for s in custom_specs)]
        protocol = create_tool_protocol(str(agent_preset.get("tool_mode") or "native"))
        messages = build_planner_messages(planner.get("layout"), texts,
                                          tool_protocol_text=protocol.tool_protocol_text(tools))
        if retry_correction:
            messages.append({"role": "user", "content": retry_correction})

        def validate(calls: list[dict[str, Any]]) -> dict[str, Any]:
            return parse_submitted_scene_plan(calls, present_characters=present, center_mode=cfg.profile.center_mode, **limits)

        runtime = PlannerRuntime(
            provider=provider, protocol=protocol, rctx=rctx, node_id=node_id,
            execute_tool=self._tool_executor(rctx, node_id),
            on_message=lambda m: self._on_message(rctx, node_id, m),
        )
        request = PlannerRequest(
            messages=messages, tools=tools, validate=validate,
            custom_tool_names=frozenset(str(s.get("name") or "") for s in custom_specs),
            max_attempts=int(planner.get("max_attempts") or 3),
            max_custom_tool_calls=int(planner.get("max_custom_tool_calls") or 5),
            timeout_sec=float(agent_preset.get("timeout_sec") or 0) or None,
        )
        return await runtime.run(request)

    async def _generate(self, rctx: Any, cfg: PlannerConfig, node_id: str,
                        result: PlannerResult) -> tuple[str, list[dict[str, Any]]]:
        planner = cfg.planner
        send_each = bool(planner.get("send_each_image", True))
        ok: list[dict[str, Any]] = []
        failed: list[str] = []
        pending_attachments: list[dict[str, Any]] = []
        api_key, base_url = cfg.api_key, cfg.base_url
        for task in result.tasks:
            check_cancelled = getattr(rctx, "check_cancelled", None)
            if check_cancelled is not None and await check_cancelled():
                failed.append(f"第 {task.get('index')} 张：任务已取消")
                break
            preset = cfg.preset_for(task.get("preset"))
            label = str(task.get("title") or f"第 {task.get('index')} 张")
            try:
                # 画幅跟着预设走（预设的 override_size + 宽高）；旧的 planner.override_size 已在读预设时并入。
                compiled = novelai.compile_task(
                    task, preset=preset, known_characters=cfg.characters, seed=task.get("seed"),
                )
                image = await novelai.generate_image(
                    compiled, api_key=api_key, base_url=base_url, workspace_root=rctx.workspace_root,
                    settings=cfg.settings,
                )
            except novelai.NovelAIError as error:
                failed.append(f"{label}：{error.message}")
                logger.warning("draw agent: 生图失败 %s code=%s", label, error.code)
                continue
            except Exception as error:  # noqa: BLE001
                failed.append(f"{label}：{type(error).__name__}: {error}")
                logger.exception("draw agent: 生图异常 %s", label)
                continue
            ok.append(image)
            self._shadow_write(rctx, node_id, {
                "role": "tool", "name": SEND_TOOL_NAME, "tool_call_id": f"draw_{uuid4().hex[:8]}",
                "content": json.dumps({"ok": True, "path": image["path"], "seed": image["seed"], "title": image["title"],
                                       "prompt": image["prompt"]}, ensure_ascii=False),
            }, MessageType.TOOL_RESULT)
            if not (send_each and await self._send_image(rctx, node_id, image["path"])):
                pending_attachments.append(image["attachment"])

        parts: list[str] = []
        if ok:
            parts.append(f"已生成 {len(ok)} 张图片" + (f"：{'、'.join(str(i.get('title') or '') for i in ok if i.get('title'))}" if any(i.get("title") for i in ok) else ""))
        if failed:
            parts.append(f"{len(failed)} 张失败：" + "；".join(failed))
        if result.argument_repair:
            parts.append("（模型返回的参数经过自动修复）")
        if result.preempt_messages:
            parts.append("收到追加消息：" + " / ".join(result.preempt_messages) + "。本次已按原计划完成，如需调整请再说一次。")
        check_preempted = getattr(rctx, "check_preempted", None)
        if check_preempted is not None:
            try:
                state = await check_preempted()
                if state and state.get("preempted"):
                    message = str(state.get("message") or "")
                    consume = getattr(rctx, "consume_preempt", None)
                    if consume is not None:
                        await consume(message, len(state.get("attachments") or []))
                    if message:
                        parts.append(f"生成期间收到追加消息「{message}」，本次未处理，如需调整请再说一次。")
            except Exception:  # noqa: BLE001
                logger.debug("draw agent: 检查打断失败", exc_info=True)
        if not parts:
            parts.append("没有生成任何图片。")
        return "，".join(parts) if len(parts) == 1 else "\n".join(parts), pending_attachments
