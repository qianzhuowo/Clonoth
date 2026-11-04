"""Local HTTP bridge used by the qq_forward tool to forward chat messages."""
from __future__ import annotations

import logging
from .config import ALLOWED_GROUPS
from .config import CLONOTH_WORKSPACE
from .config import ENABLE_FORWARD_BRIDGE
from .config import FORWARD_BRIDGE_HOST
from .config import FORWARD_BRIDGE_MAX_MESSAGES
from .config import FORWARD_BRIDGE_PORT
from .config import FORWARD_BRIDGE_TOKEN
from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Bot
from pathlib import Path
from typing import Any
from typing import Dict
import contextlib
import uuid

from . import approval, commands, message_parse, profiles, send, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

def _forward_bridge_lookup_target(session_id: str) -> Dict[str, Any] | None:
    """在内存/持久化两张表中按单个 session_id 查找目标。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    for source in (state._session_targets, state._persisted_session_targets):
        target = source.get(sid)
        if isinstance(target, dict) and target:
            return dict(target)
    return None


def _forward_bridge_session_target(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> Dict[str, Any] | None:
    """根据 Engine 传来的 session_id 找回该会话对应的真实 QQ 目标。

    2026-07-14 修复原因：入口任务实际运行在 entry branch session（branch_xxx），
    ctx.session_id 为 branch，而 _session_targets / _persisted_session_targets 只按
    用户可见的 parent session 登记。若直接拿 branch 去查会 miss，导致
    op=remind + target_type=current 在群聊中误报“当前会话不是群聊”。
    做法：依次尝试 parent_session_id -> 传入 session_id -> runtime_session_id，
    任一命中即返回，对旧调用（仅传 session_id）完全兼容。
    """
    for candidate in (parent_session_id, session_id, runtime_session_id):
        target = _forward_bridge_lookup_target(candidate)
        if target:
            return target
    return None


def _forward_bridge_origin_group_id(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> int | None:
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return None
    if str(target.get("type") or "") == "group" and target.get("group_id") is not None:
        try:
            return int(target.get("group_id"))
        except Exception:
            return None
    return None


def _forward_bridge_origin_user_id(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> int | None:
    """当前会话触发者 QQ 号，用于把内容“私发给我”。"""
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return None
    if target.get("user_id") is not None:
        try:
            return int(target.get("user_id"))
        except Exception:
            return None
    return None


def _forward_bridge_list_messages(
    session_id: str,
    query: str = "",
    limit: int = 30,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """列出当前会话（群）最近消息，供 AI 按下标/关键词多选转发。

    返回的 index 从 1 开始；只包含匿名后可展示给模型的字段。
    """
    group_id = _forward_bridge_origin_group_id(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if group_id is None:
        return []
    records = list(state._group_content_records.get(int(group_id), ()))
    if not records:
        return []
    query_norm = str(query or "").strip().lower()
    limit = max(1, min(int(limit or 30), FORWARD_BRIDGE_MAX_MESSAGES))
    items: list[dict[str, Any]] = []
    for offset, record in enumerate(records):
        line = record.formatted_line or ""
        text = record.text or ""
        if query_norm and query_norm not in line.lower() and query_norm not in text.lower():
            continue
        att_kinds = sorted({str(att.get("type") or "") for att in (record.attachments or []) if isinstance(att, dict)})
        items.append({
            "index": offset + 1,
            "preview": message_parse.compact_text(line or text, limit=200),
            "sender": state.ANON.anonymize_text(record.sender_name or ""),
            "has_image": "image" in att_kinds,
            "has_file": "file" in att_kinds,
        })
    # 只回传最近 limit 条，保留时间顺序（旧→新）。
    return items[-limit:]


def _forward_bridge_origin_bucket_key(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> str:
    """把来源会话映射为“最近发送/生成附件”索引的桶 key。"""
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return ""
    return send._sent_attachment_bucket_key(target)


def _forward_bridge_list_recent(
    session_id: str,
    *,
    only_images: bool = True,
    limit: int = 20,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """列出本会话最近由 Bot 发出/生成的附件（如生图产出的图片），带 index 下标。"""
    bucket = _forward_bridge_origin_bucket_key(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    records = send._recent_sent_attachment_records(bucket, only_images=only_images)
    limit = max(1, min(int(limit or 20), FORWARD_BRIDGE_MAX_MESSAGES))
    records = records[-limit:]
    items: list[dict[str, Any]] = []
    for offset, rec in enumerate(records):
        items.append({
            "index": offset + 1,
            "name": profiles.sanitize_name(rec.get("name") or Path(str(rec.get("path") or "")).name, max_len=60),
            "type": str(rec.get("type") or "file"),
        })
    return items


def _forward_bridge_pick_recent_attachments(
    session_id: str,
    *,
    indices: list[int] | None,
    only_images: bool = False,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """按下标从最近发送/生成附件里挑选；无下标时默认取最新一个。

    返回可直接发送的附件 dict（带绝对 path + name + type）。
    """
    bucket = _forward_bridge_origin_bucket_key(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    records = send._recent_sent_attachment_records(bucket, only_images=only_images)
    if not records:
        return []
    selected: list[dict[str, Any]]
    if indices:
        selected = []
        for raw in indices:
            try:
                idx = int(raw)
            except Exception:
                continue
            if 1 <= idx <= len(records):
                selected.append(records[idx - 1])
        if not selected:
            selected = [records[-1]]
    else:
        selected = [records[-1]]
    attachments: list[dict[str, Any]] = []
    for rec in selected:
        attachments.append({
            "type": str(rec.get("type") or "file"),
            "path": str(rec.get("path") or ""),
            "name": str(rec.get("name") or ""),
        })
    return attachments


def _forward_bridge_pick_records(
    session_id: str,
    *,
    indices: list[int] | None,
    query: str,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[state.GroupContentRecord]:
    group_id = _forward_bridge_origin_group_id(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if group_id is None:
        return []
    records = list(state._group_content_records.get(int(group_id), ()))
    if not records:
        return []
    if indices:
        picked: list[state.GroupContentRecord] = []
        for raw in indices:
            try:
                idx = int(raw)
            except Exception:
                continue
            if 1 <= idx <= len(records):
                picked.append(records[idx - 1])
        if picked:
            return picked
    query_norm = str(query or "").strip().lower()
    if query_norm:
        matched = [r for r in records if query_norm in (r.formatted_line or "").lower() or query_norm in (r.text or "").lower()]
        if matched:
            return matched[-FORWARD_BRIDGE_MAX_MESSAGES:]
    return records[-min(10, len(records)):]


async def _forward_bridge_resolve_target(
    bot: Bot,
    session_id: str,
    target_type: str,
    target_ref: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
    task_id: str = "",
) -> tuple[state.ProactiveTarget | None, str]:
    """把工具给出的目标（含 self/current 语义）解析为真实 ProactiveTarget。"""
    target_type = str(target_type or "").strip().lower()
    ref = str(target_ref or "").strip()
    if target_type == "self" or ref in {"我", "自己", "me", "self"}:
        # 群会话目标是整个群，没有发送者；按触发本次消息的根任务找回发送者。
        uid = state.task_sender(task_id) or _forward_bridge_origin_user_id(
            session_id,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        if uid is None:
            return None, "无法确定当前用户，无法私发给你。"
        return state.ProactiveTarget("private", uid, state.PROFILES.display_name(uid) or state.ANON.anonymize_user_id(uid)), ""
    if target_type == "current" or ref in {"当前群", "本群", "这个群", "此群"}:
        gid = _forward_bridge_origin_group_id(
            session_id,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        if gid is None:
            return None, "当前会话不是群聊，无法发送到本群。"
        if ALLOWED_GROUPS and gid not in ALLOWED_GROUPS:
            return None, "当前群不在允许的主动群聊目标中。"
        return state.ProactiveTarget("group", gid, "当前群"), ""
    # 其余交给既有的目标解析（支持显式 id 前缀与配置的显示名/别名）。
    resolve_type = "group" if target_type == "group" else "private"
    return await commands._resolve_proactive_target(bot, None, resolve_type, ref)


async def _forward_bridge_records_to_nodes(
    bot: Bot,
    records: list[state.GroupContentRecord],
    conversation_key: str,
    *,
    include_images: bool,
    include_files: bool,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for record in records[:FORWARD_BRIDGE_MAX_MESSAGES]:
        text = record.text or record.formatted_line or ""
        attachments: list[dict[str, Any]] = []
        if include_images or include_files:
            attachments = commands._filter_attachments_by_kind(
                record.attachments or [],
                include_images=include_images,
                include_files=include_files,
            )
        node = commands._make_forward_node(
            bot,
            text,
            attachments,
            nickname=record.sender_name or "转发消息",
            user_id=record.sender_id or getattr(bot, "self_id", None),
        )
        if node:
            nodes.append(node)
    return nodes


def _forward_bridge_resolve_files(raw_paths: Any, display_names: Any = None) -> tuple[list[dict[str, Any]], list[str]]:
    """把工具传来的仓库相对/绝对路径解析为可发送的 file 附件。

    Why: 管理员“把仓库里的 xx 文件发出来”需要把工作区内文件发到 QQ。
    How: 复用 _attachment_path_under_workspace 限定在工作区内，防止目录穿越。
    Purpose: 只允许发送工作区内存在的普通文件，逐个回报错误。
    """
    paths: list[str] = []
    if isinstance(raw_paths, str):
        paths = [raw_paths]
    elif isinstance(raw_paths, list):
        paths = [str(item) for item in raw_paths if str(item).strip()]
    names: list[str] = []
    if isinstance(display_names, str):
        names = [display_names]
    elif isinstance(display_names, list):
        names = [str(item) for item in display_names]

    attachments: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, raw in enumerate(paths):
        raw = str(raw or "").strip()
        if not raw:
            continue
        path = commands._attachment_path_under_workspace(raw)
        if path is None:
            errors.append(f"{raw}：路径必须位于 Clonoth 工作区内。")
            continue
        if not path.exists() or not path.is_file():
            errors.append(f"{raw}：文件不存在。")
            continue
        rel_path = str(path)
        try:
            rel_path = str(path.relative_to(Path(CLONOTH_WORKSPACE).resolve()))
        except Exception:
            pass
        display_name = names[idx].strip() if idx < len(names) and str(names[idx]).strip() else path.name
        attachments.append({"type": "file", "path": rel_path, "name": display_name})
    return attachments, errors


async def _forward_bridge_execute(payload: dict[str, Any]) -> dict[str, Any]:
    """执行一次自然语言转发/发送/提醒请求。仅在 Bot 进程内运行。

    The payload is mutated with ``_request_identity`` once. Re-entering the same
    request object or retrying with the same caller request_id is stable; a future
    independent request receives a new identity even when content is identical.
    """
    request_identity = str(
        payload.get("_request_identity")
        or payload.get("request_id")
        or payload.get("idempotency_key")
        or ""
    ).strip()
    if not request_identity:
        request_identity = uuid.uuid4().hex
    payload.setdefault("_request_identity", request_identity)
    try:
        bot = get_bot()
    except Exception:
        return {"ok": False, "error": "QQ Bot 尚未连接，无法执行转发。"}
    session_id = str(payload.get("session_id") or "").strip()
    # [2026-07-14] 入口任务运行在 branch session 上，因此工具会额外透传 parent/runtime
    # session id；这里全部收集下来供 target 解析做多级回退。
    parent_session_id = str(payload.get("parent_session_id") or "").strip()
    runtime_session_id = str(payload.get("runtime_session_id") or "").strip()
    action = str(payload.get("action") or "forward").strip().lower()
    target_type = str(payload.get("target_type") or "").strip().lower()
    target_ref = str(payload.get("target_ref") or "").strip()
    extra_text = approval.truncate_qq_text(str(payload.get("text") or "").strip())
    query = str(payload.get("query") or "").strip()
    include_images = bool(payload.get("include_images", True))
    include_files = bool(payload.get("include_files", True))
    raw_indices = payload.get("message_indices")
    indices: list[int] = []
    if isinstance(raw_indices, list):
        for item in raw_indices:
            try:
                indices.append(int(item))
            except Exception:
                continue
    # “最近生成/发出的图片”系列参数：use_recent 启用；recent_indices 按下标挑选。
    use_recent = bool(payload.get("use_recent", False))
    raw_recent = payload.get("recent_indices")
    recent_indices: list[int] = []
    if isinstance(raw_recent, list):
        for item in raw_recent:
            try:
                recent_indices.append(int(item))
            except Exception:
                continue
    if recent_indices:
        use_recent = True

    target, error = await _forward_bridge_resolve_target(
        bot,
        session_id,
        target_type,
        target_ref,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
        task_id=str(payload.get("task_id") or "").strip(),
    )
    if target is None:
        return {"ok": False, "error": error or "目标解析失败。"}
    send_target = commands._target_to_send_dict(target)
    label = commands._target_display_label(target.target_type, target.label)
    conversation_key = f"qq_forward:{target.target_type}:{target.target_id}"
    send_context = commands._request_send_context(
        "qq_forward", request_identity, conversation_key,
    )

    # 纯提醒/通知：不涉及历史挑选，直接发一段文本。
    if action == "remind":
        body = extra_text
        if not body:
            return {"ok": False, "error": "提醒内容为空。"}
        await send._send_text_and_attachments(
            bot, send_target, body, [], send_context=send_context,
        )
        return {"ok": True, "result": f"已向{label}发送提醒。"}

    # 发送工作区文件：“把仓库里的 xx 文件发出来”。支持一次多个文件。
    if action == "file":
        attachments, file_errors = _forward_bridge_resolve_files(
            payload.get("file_paths"),
            payload.get("file_names"),
        )
        # 允许 op=file 时不给 file_paths、而是从最近生成/发出的附件里挑（use_recent）。
        if not attachments and use_recent:
            recent_atts = _forward_bridge_pick_recent_attachments(
                session_id,
                indices=recent_indices,
                only_images=False,
                parent_session_id=parent_session_id,
                runtime_session_id=runtime_session_id,
            )
            attachments = _forward_bridge_resolve_files(
                [att.get("path") for att in recent_atts],
                [att.get("name") for att in recent_atts],
            )[0]
        if not attachments:
            hint = "；".join(file_errors) if file_errors else "请在 file_paths 里给出工作区内的文件路径，或用 use_recent 发送最近生成的图片。"
            return {"ok": False, "error": f"没有可发送的文件：{hint}"}
        if extra_text:
            await send._send_text_and_attachments(
                bot, send_target, extra_text, [], send_context=send_context,
            )
        await send._send_attachments(
            bot, send_target, attachments, send_context=send_context,
        )
        names = "、".join(profiles.sanitize_name(att.get("name") or att.get("path"), max_len=40) for att in attachments)
        result = f"已向{label}发送 {len(attachments)} 个文件：{names}"
        if file_errors:
            result += "\n（部分文件未发送：" + "；".join(file_errors) + "）"
        return {"ok": True, "result": result}

    # use_recent：把“最近生成/发出的图片”作为附件直接发送（不依赖群历史）。
    if use_recent and action in {"send", "forward"}:
        recent_atts = _forward_bridge_pick_recent_attachments(
            session_id,
            indices=recent_indices,
            only_images=False,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        recent_atts = _forward_bridge_resolve_files(
            [att.get("path") for att in recent_atts],
            [att.get("name") for att in recent_atts],
        )[0]
        if not recent_atts:
            return {"ok": False, "error": "没有找到最近生成/发送的图片。可先用 op=recent 查看可选图片。"}
        await send._send_text_and_attachments(
            bot, send_target, extra_text, recent_atts, send_context=send_context,
        )
        return {"ok": True, "result": f"已向{label}发送 {len(recent_atts)} 张最近生成/发送的图片。"}

    records = _forward_bridge_pick_records(
        session_id,
        indices=indices,
        query=query,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )

    if action == "send":
        # 直接把挑选到的消息拼成一段文本 + 附件发送（非合并转发卡片）。
        lines = [r.formatted_line or r.text for r in records if (r.formatted_line or r.text)]
        body = "\n".join(part for part in ([extra_text] if extra_text else []) + lines).strip()
        attachments: list[dict[str, Any]] = []
        if include_images or include_files:
            for r in records:
                attachments.extend(commands._filter_attachments_by_kind(
                    r.attachments or [],
                    include_images=include_images,
                    include_files=include_files,
                ))
        if not body and not attachments:
            return {"ok": False, "error": "没有可发送的内容。"}
        await send._send_text_and_attachments(
            bot, send_target, approval.truncate_qq_text(body), attachments,
            send_context=send_context,
        )
        return {"ok": True, "result": f"已发送到{label}（{len(records)} 条消息）。"}

    # 默认 action == "forward"：合并转发卡片，支持多选多条消息。
    nodes = await _forward_bridge_records_to_nodes(
        bot,
        records,
        conversation_key,
        include_images=include_images,
        include_files=include_files,
    )
    if extra_text:
        head = commands._make_forward_node(bot, extra_text, None, nickname="Clonoth 通知")
        if head:
            nodes.insert(0, head)
    if not nodes:
        return {"ok": False, "error": "没有可转发的消息。请先用 list 查看上文消息并给出 message_indices 或 query。"}
    try:
        await commands._send_forward_nodes(
            bot, target, nodes, send_context=send_context,
        )
    except Exception as exc:
        logger.warning("qq_forward bridge send forward failed: %s", exc, exc_info=True)
        return {"ok": False, "error": "合并转发发送失败：当前 OneBot/NapCat 可能不支持该接口，或目标不可达。"}
    return {"ok": True, "result": f"已向{label}发送合并转发（{len(nodes)} 条）。"}


def _forward_bridge_check_token(request: "Any") -> bool:
    if not FORWARD_BRIDGE_TOKEN:
        return True
    return request.headers.get("X-Forward-Token", "") == FORWARD_BRIDGE_TOKEN


async def _forward_bridge_http_handler(request: "Any") -> "Any":
    from aiohttp import web  # 局部导入，未启用 Bridge 时不强依赖 aiohttp。

    if not _forward_bridge_check_token(request):
        return web.json_response({"ok": False, "error": "invalid token"}, status=403)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid payload"}, status=400)
    caller_request_id = str(
        payload.get("request_id")
        or request.headers.get("Idempotency-Key", "")
        or request.headers.get("X-Request-ID", "")
        or uuid.uuid4().hex
    ).strip()
    # The private field is internal-only: always overwrite it with the identity from
    # the public HTTP contract so a retry cannot accidentally fork the send claim.
    payload["request_id"] = caller_request_id
    payload["_request_identity"] = caller_request_id
    op = str(payload.get("op") or "").strip().lower()
    try:
        _parent_sid = str(payload.get("parent_session_id") or "")
        _runtime_sid = str(payload.get("runtime_session_id") or "")
        if op == "list":
            messages = _forward_bridge_list_messages(
                str(payload.get("session_id") or ""),
                query=str(payload.get("query") or ""),
                limit=int(payload.get("limit") or FORWARD_BRIDGE_MAX_MESSAGES),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
            )
            return web.json_response({"ok": True, "messages": messages})
        if op == "recent":
            recent = _forward_bridge_list_recent(
                str(payload.get("session_id") or ""),
                only_images=bool(payload.get("only_images", True)),
                limit=int(payload.get("limit") or 20),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
            )
            return web.json_response({"ok": True, "recent": recent})
        if op in {"forward", "send", "remind", "file", ""}:
            if op:
                payload.setdefault("action", op)
            result = await _forward_bridge_execute(payload)
            status = 200 if result.get("ok") else 400
            return web.json_response(result, status=status)
        return web.json_response({"ok": False, "error": f"unknown op: {op}"}, status=400)
    except Exception as exc:
        logger.warning("qq_forward bridge handler error: %s", exc, exc_info=True)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def _start_forward_bridge() -> None:
    """在 QQ Bot 进程内启动 qq_forward Bridge Server。"""
    if state._forward_bridge_started or not ENABLE_FORWARD_BRIDGE:
        return
    try:
        from aiohttp import web
    except Exception:
        logger.warning("qq_forward bridge disabled: aiohttp 未安装。")
        return
    app = web.Application()
    app.router.add_post("/qq_forward", _forward_bridge_http_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, FORWARD_BRIDGE_HOST, FORWARD_BRIDGE_PORT)
    await site.start()
    state._forward_bridge_runner = runner
    state._forward_bridge_site = site
    state._forward_bridge_started = True
    logger.info(
        "qq_forward bridge server started: http://%s:%s/qq_forward (token=%s)",
        FORWARD_BRIDGE_HOST,
        FORWARD_BRIDGE_PORT,
        "set" if FORWARD_BRIDGE_TOKEN else "none",
    )


async def _stop_forward_bridge() -> None:
    if state._forward_bridge_runner is not None:
        with contextlib.suppress(Exception):
            await state._forward_bridge_runner.cleanup()
    state._forward_bridge_runner = None
    state._forward_bridge_site = None
    state._forward_bridge_started = False
