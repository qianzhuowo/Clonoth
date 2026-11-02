"""QQ admin commands: drawing, model switch, proactive send, clear group memory."""
from __future__ import annotations

import logging
from .config import ADMIN_QQ_USERS
from .config import ALLOWED_GROUPS
from .config import ALLOWED_PRIVATE_USERS
from .config import ALLOW_PRIVATE_FRIENDS
from .config import CLONOTH_WORKSPACE
from .send_contract import IdempotencyClaim
from .send_contract import OutboundSendContext
from .send_contract import make_idempotency_key
from .send_contract import protected_claim_send
from collections import defaultdict
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11 import Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
import asyncio
import contextlib
import hashlib
import json
import re
import sys
import uuid

from . import access, inbound_attachments, message_parse, profiles, send, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

def _forward_nodes_from_cached_reply(bot: Bot, reply_message_id: Any) -> list[dict[str, Any]]:
    mid = str(reply_message_id or "").strip()
    if not mid:
        return []
    item = state.REPLY_CACHE.get(mid)
    if not isinstance(item, dict):
        return []
    attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
    if not attachments:
        return []
    node = _make_forward_node(
        bot,
        "",
        [dict(att) for att in attachments if isinstance(att, dict)],
        nickname="引用图片",
        user_id=item.get("sender_id") or getattr(bot, "self_id", "") or "10000",
    )
    return [node] if node else []


def _drawtools_help_text() -> str:
    return (
        "【NovelAI 生图命令】\n"
        "1) /生图 画一个穿着西装的帅哥\n"
        "   直接进入绘图节点并生成图片。\n"
        "2) /生图 初音未来，画风用可爱风\n"
        "   可在需求里指定画风/预设。\n"
        "3) /生图 给我初音未来的NAI提示词，不要画\n"
        "   只输出绘图 tag / prompt，不调用生图。\n"
        "4) /画师串列表\n"
        "   查看可用绘图预设。\n"
        "5) /切换画师串 可爱风\n"
        "   切换默认绘图预设。\n"
        "支持别名：/画图、/绘图、/nai、/draw。"
    )


def _load_drawtools_preset_manager():
    drawtools_dir = Path(CLONOTH_WORKSPACE) / "tools" / "drawtools"
    if str(drawtools_dir) not in sys.path:
        sys.path.insert(0, str(drawtools_dir))
    from preset_manager import list_presets, switch_preset  # type: ignore
    return list_presets, switch_preset


async def _maybe_handle_drawtools_command(user_text: str) -> str | None:
    """处理 QQ 侧绘图帮助/预设命令；返回回复文本，None 表示不是命令。"""
    text = (user_text or "").strip()
    if not text:
        return None
    if state._DRAW_HELP_RE.match(text):
        return _drawtools_help_text()
    if state._DRAW_PRESET_LIST_RE.match(text):
        try:
            list_presets, _switch_preset = _load_drawtools_preset_manager()
            presets = list_presets()
        except Exception as exc:
            logger.warning("list draw presets failed: %s", exc, exc_info=True)
            return f"读取绘图预设失败：{exc}"
        if not presets:
            return "当前没有可用绘图预设。"
        lines = []
        for item in presets:
            mark = "✅ " if item.get("selected") else "   "
            lines.append(f"{mark}{item.get('name') or item.get('id')}（id: {item.get('id')}，model: {item.get('model')}，CFG: {item.get('scale')}，steps: {item.get('steps')}）")
        return "【可用画师串/绘图预设】\n" + "\n".join(lines) + "\n切换：/切换画师串 <名称或id>"
    switch_match = state._DRAW_PRESET_SWITCH_RE.match(text)
    if switch_match:
        preset_ref = switch_match.group(1).strip()
        try:
            _list_presets, switch_preset = _load_drawtools_preset_manager()
            preset = switch_preset(preset_ref)
        except Exception as exc:
            logger.warning("switch draw preset failed: %s", exc, exc_info=True)
            return f"切换绘图预设失败：没找到“{preset_ref}”，可发送 /画师串列表 查看可用项。"
        return f"已切换默认画师串：{preset.get('name') or preset.get('id')}（id: {preset.get('id')}）"
    return None


async def _maybe_handle_model_command(
    *,
    event: Event,
    user_text: str,
) -> str | None:
    """处理 QQ 管理员 /切换模型 <模型名> 命令，切换全局主模型。

    返回回复文本；None 表示不是本命令。切换通过 Supervisor 的
    POST /v1/config/openai 实时写入 data/config.yaml；Engine 每次处理任务
    前都会重新拉取该配置（fetch_openai_secret），因此无需重启服务。
    """
    text = (user_text or "").strip()
    if not text:
        return None

    if state._client is None:
        # 不是本命令时返回 None；确实是本命令但客户端未就绪时才提示。
        if state._MODEL_SWITCH_RE.match(text) or state._MODEL_SHOW_RE.match(text):
            return "Clonoth Agent 尚未初始化，请稍后重试。"
        return None

    is_admin = access.is_admin_user(getattr(event, "user_id", ""))

    # 查看当前模型 / 帮助（仅管理员，避免向普通用户暴露底层模型名）。
    if state._MODEL_SHOW_RE.match(text):
        if not is_admin:
            return "模型命令仅限 Clonoth 管理员使用。"
        try:
            cfg = await state._client.get_openai_config()
        except Exception as exc:
            logger.warning("get openai config failed: %s", exc, exc_info=True)
            return f"❌ 获取当前模型失败：{exc}"
        return (
            f"ℹ️ 当前 model → {cfg.model or '(未设置)'}\n"
            "切换：/切换模型 <模型名>"
        )

    switch_match = state._MODEL_SWITCH_RE.match(text)
    if switch_match:
        if not is_admin:
            # 不向非管理员暴露切换能力。
            return "模型命令仅限 Clonoth 管理员使用。"
        model_name = switch_match.group(1).strip()
        # 去掉可能的包裹引号/反引号。
        model_name = model_name.strip("`\"'").strip()
        if not model_name:
            return "用法：/切换模型 <模型名>"
        try:
            out = await state._client.update_openai_config(model=model_name)
        except Exception as exc:
            logger.warning("switch global model failed: %s", exc, exc_info=True)
            return f"❌ 切换失败：{exc}"
        new_model = ""
        try:
            new_model = str((out or {}).get("openai", {}).get("model") or "").strip()
        except Exception:
            new_model = ""
        logger.info("QQ admin %s switched global model -> %s", getattr(event, "user_id", ""), new_model or model_name)
        return f"✅ model → {new_model or model_name}"

    return None


def _normalize_target_ref(text: str) -> str:
    """把管理员输入的目标名规整为可匹配 token，不写入模型上下文。"""
    return re.sub(r"[\s_\-—]+", "", str(text or "").strip().lower())


def _proactive_help_text() -> str:
    return (
        "【管理员主动发送 / 转发命令】\n"
        "权限：仅 CLONOTH_ADMIN_QQ_USERS 中的管理员可用；命令在 QQ 适配器本地处理，不进入普通模型上下文。\n"
        "目标：使用联系人显示名/好友备注/群名/配置别名；目标列表不会展示真实 QQ 号。\n\n"
        "1) 查看可用目标\n"
        "   主动目标 / 主动目标 私聊 / 主动目标 群\n"
        "2) 主动发文本（可在同条消息附图，图片会一起发送）\n"
        "   私信 <联系人名> <内容>\n"
        "   群发 <群名> <内容>\n"
        "   发送 私聊 <联系人名> <内容>\n"
        "   发送 群 <群名> <内容>\n"
        "3) 主动发本地文件（路径限制在 Clonoth 工作区内，推荐 data/attachments/）\n"
        "   发文件 私聊 <联系人名> data/attachments/xxx.png\n"
        "   发文件 群 <群名> data/attachments/xxx.zip 展示文件名.zip\n"
        "4) 合并转发\n"
        "   合并转发 私聊 <联系人名> <内容>\n"
        "   合并转发 群 <群名> <内容>\n"
        "   也可以引用一条消息/合并转发卡片后发送：合并转发 群 <群名>\n"
        "   文本中用单独一行 --- 可拆成多条转发 node。\n\n"
        "5) 清除群记忆（清空指定 QQ 群的长期记忆）\n"
        "   /清除群记忆            —— 群聊内直接清空当前群；私聊内列出可清理的群\n"
        "   /清除群记忆 <群名>     —— 清空指定群（群名/别名/群号均可）"
    )


def _parse_proactive_command(text: str) -> dict[str, str] | None:
    """解析管理员主动发送/转发命令。

    返回字段：action(send/file/forward/list/help), target_type(private/group), target_ref, body。
    目标名按单个 token 解析；如群名含空格，请在配置里设置无空格别名/显示名。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if raw in {"主动发送帮助", "主动转发帮助", "通知帮助", "转发帮助"}:
        return {"action": "help"}
    parts = raw.split()
    if not parts:
        return None
    head = parts[0]
    if head in {"主动目标", "通知目标", "转发目标", "目标列表"}:
        kind = parts[1] if len(parts) >= 2 else ""
        return {"action": "list", "target_type": _canonical_proactive_target_type(kind)}
    if head in {"私信", "私聊通知"} and len(parts) >= 3:
        return {"action": "send", "target_type": "private", "target_ref": parts[1], "body": raw.split(None, 2)[2]}
    if head in {"群发", "群通知"} and len(parts) >= 3:
        return {"action": "send", "target_type": "group", "target_ref": parts[1], "body": raw.split(None, 2)[2]}
    if head in {"发送", "通知", "主动发送"} and len(parts) >= 4:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            return {"action": "send", "target_type": target_type, "target_ref": parts[2], "body": raw.split(None, 3)[3]}
    if head in {"发文件", "发送文件"} and len(parts) >= 4:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3]
            return {"action": "file", "target_type": target_type, "target_ref": parts[2], "body": body}
    if head in {"合并转发", "转发"} and len(parts) >= 3:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3] if len(parts) >= 4 else ""
            return {"action": "forward", "target_type": target_type, "target_ref": parts[2], "body": body}
    if head in {"转发到", "转发给", "合并转发到", "合并转发给"} and len(parts) >= 3:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3] if len(parts) >= 4 else ""
            return {"action": "forward", "target_type": target_type, "target_ref": parts[2], "body": body}
    # English aliases for admins that copy/paste commands.
    if lowered.startswith("send ") and len(parts) >= 4:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            return {"action": "send", "target_type": target_type, "target_ref": parts[2], "body": raw.split(None, 3)[3]}
    if lowered.startswith("forward ") and len(parts) >= 3:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3] if len(parts) >= 4 else ""
            return {"action": "forward", "target_type": target_type, "target_ref": parts[2], "body": body}
    return None


def _canonical_proactive_target_type(kind: str) -> str:
    token = str(kind or "").strip().lower()
    if not token:
        return ""
    if token in state._PROACTIVE_PRIVATE_KINDS:
        return "private"
    if token in state._PROACTIVE_GROUP_KINDS:
        return "group"
    return ""


def _target_display_label(target_type: str, name: str) -> str:
    prefix = "私聊" if target_type == "private" else "群聊"
    return f"{prefix}「{profiles.sanitize_name(name, max_len=40)}」"


async def _safe_call_onebot_list(bot: Bot, api_name: str) -> list[dict[str, Any]]:
    try:
        data = await bot.call_api(api_name)
    except Exception:
        logger.debug("OneBot list API failed: %s", api_name, exc_info=True)
        return []
    payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else data
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


async def _private_target_candidates(bot: Bot) -> list[state.ProactiveTarget]:
    """获取管理员可主动私聊的目标；返回值不包含真实 QQ 字符串展示。"""
    friend_rows = await _safe_call_onebot_list(bot, "get_friend_list") if ALLOW_PRIVATE_FRIENDS else []
    friend_ids: set[int] = set()
    names_by_id: dict[int, list[str]] = defaultdict(list)
    for row in friend_rows:
        try:
            uid = int(row.get("user_id"))
        except Exception:
            continue
        friend_ids.add(uid)
        for key in ("remark", "nickname", "card"):
            name = str(row.get(key) or "").strip()
            if name:
                names_by_id[uid].append(name)
    allowed_ids = set(ADMIN_QQ_USERS) | set(ALLOWED_PRIVATE_USERS) | friend_ids
    # 只把 profile 作为别名来源；是否允许发送仍由 allowed_ids/friend list 决定。
    for raw_uid in state.PROFILES.profiles:
        try:
            uid = int(raw_uid)
        except Exception:
            continue
        if uid in allowed_ids:
            names_by_id[uid].extend(state.PROFILES.aliases(uid))
    candidates: list[state.ProactiveTarget] = []
    for uid in sorted(allowed_ids):
        aliases = [a for a in dict.fromkeys(names_by_id.get(uid, []) + state.PROFILES.aliases(uid)) if a]
        label = aliases[0] if aliases else f"User{len(candidates) + 1}"
        candidates.append(state.ProactiveTarget("private", uid, label))
    return candidates


async def _group_target_candidates(bot: Bot) -> list[state.ProactiveTarget]:
    rows = await _safe_call_onebot_list(bot, "get_group_list")
    allowed = set(ALLOWED_GROUPS)
    candidates: list[state.ProactiveTarget] = []
    seen: set[int] = set()
    for row in rows:
        try:
            gid = int(row.get("group_id"))
        except Exception:
            continue
        if allowed and gid not in allowed:
            continue
        seen.add(gid)
        name = str(row.get("group_name") or row.get("group_remark") or "").strip()
        candidates.append(state.ProactiveTarget("group", gid, profiles.sanitize_name(name or f"Group{len(candidates) + 1}", max_len=40)))
    # 若 get_group_list 不可用，也允许配置白名单群通过“当前群/群名缓存”之外的显式候选参与解析。
    for gid in allowed:
        if gid not in seen:
            candidates.append(state.ProactiveTarget("group", int(gid), state.ANON.anonymize_group_id(gid)))
    return candidates


def _candidate_alias_tokens(target: state.ProactiveTarget) -> set[str]:
    tokens = {_normalize_target_ref(target.label)}
    if target.target_type == "private":
        for alias in state.PROFILES.aliases(target.target_id):
            tokens.add(_normalize_target_ref(alias))
    elif target.target_type == "group":
        tokens.add(_normalize_target_ref(state.ANON.anonymize_group_id(target.target_id)))
    return {t for t in tokens if t}


async def _resolve_proactive_target(bot: Bot, event: Event, target_type: str, ref: str) -> tuple[state.ProactiveTarget | None, str]:
    ref_text = str(ref or "").strip()
    if not ref_text:
        return None, "缺少目标名称。"
    ref_norm = _normalize_target_ref(ref_text)
    # 管理员显式输入 id 时允许解析，但目标列表/模型上下文不会展示真实 QQ 号。
    explicit = ref_text
    for prefix in ("qq:", "user:", "u:", "群:", "group:", "g:"):
        if explicit.lower().startswith(prefix):
            explicit = explicit[len(prefix):]
            break
    if explicit.isdigit():
        target_id = int(explicit)
        if target_type == "group":
            if ALLOWED_GROUPS and target_id not in ALLOWED_GROUPS:
                return None, "该群不在允许的主动群聊目标中。请先加入 CLONOTH_ALLOWED_GROUPS。"
            return state.ProactiveTarget("group", target_id, state.ANON.anonymize_group_id(target_id)), ""
        allowed_private_ids = {t.target_id for t in await _private_target_candidates(bot)}
        if target_id not in allowed_private_ids:
            return None, "该私聊目标不在好友/管理员/允许私聊白名单中。"
        return state.ProactiveTarget("private", target_id, state.PROFILES.display_name(target_id) or state.ANON.anonymize_user_id(target_id)), ""

    if target_type == "group" and ref_norm in {_normalize_target_ref("当前群"), _normalize_target_ref("本群")} and isinstance(event, GroupMessageEvent):
        gid = int(event.group_id)
        if ALLOWED_GROUPS and gid not in ALLOWED_GROUPS:
            return None, "当前群不在允许的主动群聊目标中。"
        return state.ProactiveTarget("group", gid, "当前群"), ""

    candidates = await (_private_target_candidates(bot) if target_type == "private" else _group_target_candidates(bot))
    matches = [target for target in candidates if ref_norm in _candidate_alias_tokens(target)]
    if len(matches) == 1:
        return matches[0], ""
    if not matches:
        kind = "私聊" if target_type == "private" else "群聊"
        return None, f"没有找到{kind}目标：{ref_text}\n可发送“主动目标 {kind}”查看可用名称。"
    labels = "、".join(profiles.sanitize_name(m.label, max_len=24) for m in matches[:10])
    return None, f"目标名称不唯一：{ref_text}\n匹配到：{labels}\n请在配置中设置唯一显示名，或使用管理员显式 id 前缀。"


async def _proactive_target_list_text(bot: Bot, target_type: str = "") -> str:
    sections: list[str] = []
    if target_type in ("", "private"):
        privates = await _private_target_candidates(bot)
        names = [profiles.sanitize_name(t.label, max_len=30) for t in privates if t.label]
        sections.append("【可主动私聊目标】\n" + ("、".join(names[:80]) if names else "（无；需好友列表、管理员或允许私聊白名单）"))
    if target_type in ("", "group"):
        groups = await _group_target_candidates(bot)
        names = [profiles.sanitize_name(t.label, max_len=30) for t in groups if t.label]
        sections.append("【可主动群聊目标】\n" + ("、".join(names[:80]) if names else "（无；需配置 CLONOTH_ALLOWED_GROUPS 或 get_group_list 可用）"))
    return "\n\n".join(sections) + "\n\n提示：目标列表不展示真实 QQ 号；普通模型上下文也不会接触这些真实 id。"


def _target_to_send_dict(target: state.ProactiveTarget) -> Dict[str, Any]:
    if target.target_type == "private":
        return {"type": "private", "user_id": target.target_id}
    return {"type": "group", "group_id": target.target_id}


def _attachment_path_under_workspace(raw_path: str) -> Path | None:
    raw = str(raw_path or "").strip()
    if not raw:
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
    path = Path(raw)
    if not path.is_absolute():
        path = Path(CLONOTH_WORKSPACE) / path
    try:
        resolved = path.resolve()
        workspace = Path(CLONOTH_WORKSPACE).resolve()
        if workspace not in resolved.parents and resolved != workspace:
            return None
    except Exception:
        return None
    return resolved


def _parse_file_send_body(body: str) -> tuple[Dict[str, Any] | None, str]:
    parts = str(body or "").strip().split(maxsplit=1)
    if not parts:
        return None, "缺少文件路径。"
    path = _attachment_path_under_workspace(parts[0])
    if path is None:
        return None, "文件路径必须位于 Clonoth 工作区内。"
    if not path.exists() or not path.is_file():
        return None, f"文件不存在：{parts[0]}"
    display_name = parts[1].strip() if len(parts) > 1 else path.name
    rel_path = str(path)
    try:
        rel_path = str(path.relative_to(Path(CLONOTH_WORKSPACE).resolve()))
    except Exception:
        pass
    return {"type": "file", "path": rel_path, "name": display_name}, ""


def _forward_media_text(message: Any, bot_self_id: Any = None) -> str:
    """为合并转发提取文本，去掉会重复出现的图片/表情占位符。"""
    text = message_parse.message_to_text(message, state.at_name, bot_self_id)
    if inbound_attachments.iter_image_sources(message):
        # 合并转发 node 会单独携带 image segment；这里去掉模型可读占位，避免
        # 转发卡片里出现“[图片]”文字后面又跟真实图片。
        text = re.sub(r"\[(?:图片|mface|marketface|表情包|动画表情)\]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
    return text


def _forward_content_segments(text: str = "", attachments: list[dict[str, Any]] | None = None, image_urls: list[str] | None = None) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    if text:
        segments.append({"type": "text", "data": {"text": str(text)}})
    for url in image_urls or []:
        if url:
            segments.append({"type": "image", "data": {"file": str(url)}})
    for att in attachments or []:
        path = send._resolve_attachment_path(att)
        if path and path.exists() and path.suffix.lower() in inbound_attachments.IMAGE_SUFFIXES:
            segments.append({"type": "image", "data": {"file": f"file://{str(path.resolve())}"}})
        elif att:
            name = send._attachment_filename(att) or "附件"
            segments.append({"type": "text", "data": {"text": f"[附件: {name}]"}})
    return segments


def _make_forward_node(bot: Bot, text: str = "", attachments: list[dict[str, Any]] | None = None, *, nickname: str = "Clonoth 通知", user_id: Any = None, image_urls: list[str] | None = None) -> dict[str, Any] | None:
    content = _forward_content_segments(text, attachments, image_urls)
    if not content:
        return None
    return {
        "type": "node",
        "data": {
            "user_id": str(user_id or getattr(bot, "self_id", "") or "10000"),
            "nickname": profiles.sanitize_name(nickname, max_len=32),
            "content": content,
        },
    }


async def _forward_messages_to_nodes(bot: Bot, messages: list[dict[str, Any]], conversation_key: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for item in messages[:state._FORWARD_MSG_MAX_MESSAGES]:
        sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
        sender_id = sender.get("user_id") or item.get("user_id") or getattr(bot, "self_id", "")
        nickname = sender.get("card") or sender.get("nickname") or "转发消息"
        content = item.get("content") if item.get("content") is not None else item.get("message")
        text = _forward_media_text(content, getattr(bot, "self_id", None))
        image_urls = inbound_attachments.iter_image_sources(content)
        attachments: list[dict[str, Any]] = []
        if image_urls:
            attachments, _errors = await inbound_attachments.image_sources_to_attachments(image_urls, conversation_key)
        node = _make_forward_node(bot, text, attachments, nickname=nickname, user_id=sender_id)
        if node:
            nodes.append(node)
    return nodes


async def _forward_nodes_from_reply(bot: Bot, event: Event, conversation_key: str) -> list[dict[str, Any]]:
    reply_message_id = message_parse.extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    reply_obj = getattr(event, "reply", None)
    reply: Dict[str, Any] | None = None
    if reply_obj is not None:
        # NapCat/NoneBot 的私聊引用有时无法再通过 get_msg 取回，但事件本身
        # 已携带 reply 对象；主动转发优先使用事件内 reply，避免“引用了但无内容”。
        reply = {
            "message": getattr(reply_obj, "message", None),
            "raw_message": getattr(reply_obj, "raw_message", None),
            "sender": getattr(reply_obj, "sender", None),
            "user_id": getattr(reply_obj, "user_id", None),
            "time": getattr(reply_obj, "time", None),
        }
    if (not reply or reply.get("message") is None) and reply_message_id is not None:
        cached = state.REPLY_MSGS.get(reply_message_id)
        if cached:
            reply = cached
    if (not reply or reply.get("message") is None) and reply_message_id is not None:
        reply = await message_parse.get_reply_message(bot, reply_message_id)
    if not reply:
        cached_nodes = _forward_nodes_from_cached_reply(bot, reply_message_id)
        if cached_nodes:
            return cached_nodes
        logger.info("forward reply skipped: no reply object/id=%s", reply_message_id)
        return []
    message = reply.get("message") if reply.get("message") is not None else reply.get("raw_message")
    forward_ids = message_parse.extract_forward_ids(message)
    if forward_ids:
        nodes: list[dict[str, Any]] = []
        for forward_id in forward_ids[:3]:
            messages = await message_parse.get_forward_messages(bot, forward_id)
            if messages:
                nodes.extend(await _forward_messages_to_nodes(bot, messages, conversation_key))
        if nodes:
            return nodes
    sender = reply.get("sender") if isinstance(reply.get("sender"), dict) else {}
    sender_id = sender.get("user_id") or reply.get("user_id") or getattr(bot, "self_id", "")
    nickname = sender.get("card") or sender.get("nickname") or "引用消息"
    text = _forward_media_text(message, getattr(bot, "self_id", None))
    image_urls = inbound_attachments.iter_image_sources(message)
    attachments: list[dict[str, Any]] = []
    if image_urls:
        attachments, _errors = await inbound_attachments.image_sources_to_attachments(image_urls, conversation_key)
        if attachments and reply_message_id is not None:
            state.REPLY_CACHE.remember(reply_message_id, conversation_key, str(sender_id or ""), attachments)
    if not attachments and reply_message_id is not None:
        cached_nodes = _forward_nodes_from_cached_reply(bot, reply_message_id)
        if cached_nodes:
            return cached_nodes
    node = _make_forward_node(bot, text, attachments, nickname=nickname, user_id=sender_id)
    return [node] if node else []


def _request_send_context(
    scope: str, request_identity: str, conversation_key: str = "",
) -> OutboundSendContext:
    digest = hashlib.sha256(request_identity.encode("utf-8", "ignore")).hexdigest()
    return OutboundSendContext(
        conversation_key=conversation_key,
        idempotency_key=f"request:{scope}:{digest}",
    )


async def _send_forward_nodes(
    bot: Bot,
    target: state.ProactiveTarget,
    nodes: list[dict[str, Any]],
    *,
    send_context: OutboundSendContext | None = None,
) -> None:
    target_dict = _target_to_send_dict(target)
    identity = "forward-nodes:" + hashlib.sha256(
        json.dumps(nodes, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    context = (send_context or OutboundSendContext()).child(identity)
    key = make_idempotency_key(
        target_dict, identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await state._outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await state._outbound_idempotency.wait_for_resolution(key)
        claim = (
            await state._outbound_idempotency.begin(key)
            if resolved is None else IdempotencyClaim(key, False, resolved)
        )
    if not claim.acquired:
        return
    heartbeat = asyncio.create_task(send._heartbeat_idempotency_claim(claim))
    try:
        async def send_forward() -> Any:
            if target.target_type == "group":
                return await bot.call_api(
                    "send_group_forward_msg", group_id=int(target.target_id), messages=nodes,
                )
            return await bot.call_api(
                "send_private_forward_msg", user_id=int(target.target_id), messages=nodes,
            )

        await protected_claim_send(
            state._outbound_idempotency,
            claim,
            send_forward,
            sent_ttl=send._sent_ttl_for_context(context),
            message_id_getter=send._extract_sent_message_id,
        )
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


def _filter_attachments_by_kind(
    attachments: List[Dict[str, Any]],
    *,
    include_images: bool,
    include_files: bool,
) -> List[Dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("type") or "")
        if kind == "image" and include_images:
            selected.append(dict(att))
        elif kind == "file" and include_files:
            selected.append(dict(att))
        elif include_images and include_files:
            selected.append(dict(att))
    return selected


async def _maybe_handle_proactive_command(
    *,
    bot: Bot,
    event: Event,
    user_text: str,
    conversation_key: str,
    current_attachments: List[Dict[str, Any]],
) -> str | None:
    """处理管理员主动私聊/群聊发送、文件发送、合并转发等显式命令请求。

    注意：这里只处理带明确命令前缀（如“合并转发/私信/群发/转发到”）的结构化命令。
    自然语言转发需求（例如“把上面聊到的 xxx 转发给我”）不在此拦截，而是原样交给
    AI（qq.orchestrator 节点）分析，由其调用 qq_forward 工具挑选并转发对应条目，
    避免与“让 QQ Bot 分析哪些条目需要转发”的初衷冲突。
    """
    command = _parse_proactive_command(user_text)
    is_admin = access.is_admin_user(getattr(event, "user_id", ""))
    if command is None:
        return None
    if not is_admin:
        # 不向非管理员暴露“主动发送/转发”能力、目标列表或命令名称。
        return "该请求不可用。"
    action = command.get("action") or ""
    if action == "help":
        return _proactive_help_text()
    if action == "list":
        return await _proactive_target_list_text(bot, command.get("target_type") or "")

    target_type = command.get("target_type") or ""
    target, error = await _resolve_proactive_target(bot, event, target_type, command.get("target_ref") or "")
    if target is None:
        return error or "目标解析失败。"
    send_target = _target_to_send_dict(target)
    label = _target_display_label(target.target_type, target.label)
    request_id = str(getattr(event, "message_id", "") or uuid.uuid4().hex)
    send_context = _request_send_context(
        "proactive", f"{conversation_key}:{request_id}", conversation_key,
    )

    if action == "send":
        body = str(command.get("body") or "").strip()
        attachments = current_attachments or []
        if not body and not attachments:
            return "发送内容为空。"
        await send._send_text_and_attachments(
            bot, send_target, body, attachments, send_context=send_context,
        )
        return f"已发送到{label}。"

    if action == "file":
        attachment, file_error = _parse_file_send_body(command.get("body") or "")
        if attachment is None:
            return file_error
        await send._send_attachments(
            bot, send_target, [attachment], send_context=send_context,
        )
        return f"已向{label}发送文件：{attachment.get('name') or attachment.get('path')}"

    if action == "forward":
        body = str(command.get("body") or "").strip()
        payload_attachments = current_attachments or []
        nodes: list[dict[str, Any]] = []
        if body:
            chunks = [chunk.strip() for chunk in re.split(r"\n\s*---\s*\n", body) if chunk.strip()]
            for chunk in chunks or [body]:
                node = _make_forward_node(bot, chunk, payload_attachments if not nodes else None)
                if node:
                    nodes.append(node)
        if not nodes:
            nodes = await _forward_nodes_from_reply(bot, event, conversation_key)
        if not nodes and payload_attachments:
            node = _make_forward_node(bot, "", payload_attachments)
            if node:
                nodes.append(node)
        if not nodes:
            return "没有可转发内容。请提供文本，或引用一条消息/合并转发卡片。"
        try:
            await _send_forward_nodes(
                bot, target, nodes, send_context=send_context,
            )
        except Exception as exc:
            logger.warning("send forward message failed: %s", exc, exc_info=True)
            return "合并转发发送失败：当前 OneBot/NapCat 可能不支持该接口，或目标不可达。"
        return f"已向{label}发送合并转发（{len(nodes)} 条 node）。"

    return None


def _parse_clear_group_memory_command(text: str) -> Optional[str]:
    """识别 /清除群记忆 命令。返回目标群引用（可为空字符串表示当前群/需列表）。

    返回 None 表示不是本命令。返回 "" 表示命令无参数；返回非空字符串表示
    管理员显式指定了群名/群号。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    match = state._CLEAR_GROUP_MEMORY_RE.match(raw)
    if match is None:
        return None
    return match.group(1).strip()


def _conversation_memory_namespace_for_group(group_id: int) -> str:
    """计算 QQ 群对应的 memory namespace 目录名。

    必须与 engine.builtin.knowledge_inject._conversation_memory_namespace 完全一致：
    对"真实 conversation_key"（qq_group:<群号>）做纯 SHA256 后取前 24 位，前缀 conv_。
    注意这里用真实 key 而非稳定哈希 key，因为 memory 落盘用的就是真实 key 的摘要。
    """
    real_key = f"qq_group:{int(group_id)}"
    digest = hashlib.sha256(real_key.encode("utf-8")).hexdigest()[:24]
    return f"conv_{digest}"


def _clear_group_memory_namespace(group_id: int) -> tuple[bool, int]:
    """删除指定群的 memory namespace 目录及其命中缓存条目。

    返回 (是否存在过目录, 删除的 memory book 文件数)。删除后清除 knowledge
    injector 的进程内缓存与磁盘 .hit_cache.json 中对应条目，避免残留。
    """
    namespace = _conversation_memory_namespace_for_group(group_id)
    mem_root = Path(CLONOTH_WORKSPACE) / "data" / "memory"
    ns_dir = mem_root / namespace
    deleted_books = 0
    existed = ns_dir.exists() and ns_dir.is_dir()
    if existed:
        try:
            deleted_books = sum(1 for _ in ns_dir.glob("*.yaml"))
        except Exception:
            deleted_books = 0
        import shutil as _shutil
        try:
            _shutil.rmtree(ns_dir)
        except Exception:
            logger.exception("remove memory namespace failed: %s", ns_dir)
            existed = False
    # 尽力使 engine 侧 memory catalog 缓存与磁盘命中缓存失效，避免被删条目仍被注入。
    try:
        from engine.builtin.knowledge_inject import _invalidate_cache as _ki_invalidate  # type: ignore
        _ki_invalidate(Path(CLONOTH_WORKSPACE), memory_book=namespace)
    except Exception:
        logger.debug("invalidate knowledge cache failed for %s", namespace, exc_info=True)
    _drop_hit_cache_entries()
    return existed, deleted_books


def _drop_hit_cache_entries() -> None:
    """删除 data/memory/.hit_cache.json，让被清群的命中记录一并作废。

    命中缓存以 entry id 为 key、不区分 namespace，被清群的 entry id 已随目录
    删除；直接移除整个命中缓存文件最简单且安全（下次访问会重建）。
    """
    hit_cache = Path(CLONOTH_WORKSPACE) / "data" / "memory" / ".hit_cache.json"
    try:
        if hit_cache.exists():
            hit_cache.unlink()
    except Exception:
        logger.debug("drop hit cache failed", exc_info=True)


async def _clearable_group_targets(bot: Bot) -> list[state.ProactiveTarget]:
    """列出管理员可清理记忆的群（复用主动群目标候选逻辑）。"""
    return await _group_target_candidates(bot)


async def _clear_group_memory_list_text(bot: Bot) -> str:
    """生成私聊场景下"可清理群记忆"的群名列表提示。"""
    groups = await _clearable_group_targets(bot)
    names = [profiles.sanitize_name(t.label, max_len=30) for t in groups if t.label]
    body = "、".join(names[:80]) if names else "（无；需配置 CLONOTH_ALLOWED_GROUPS 或 get_group_list 可用）"
    return (
        "【可清理群记忆的群】\n"
        + body
        + "\n\n用法：/清除群记忆 <群名>\n"
        "例如：/清除群记忆 " + (names[0] if names else "某群")
        + "\n提示：群名可用 get_group_list 中的群名或配置别名；也可用 /清除群记忆 群号"
    )


async def _maybe_handle_clear_group_memory_command(
    *,
    bot: Bot,
    event: Event,
    user_text: str,
) -> str | None:
    """处理管理员 /清除群记忆 命令。

    - 群聊中无参数：清空当前群记忆。
    - 私聊中无参数：返回可清理群列表 + 用法。
    - 任意场景带参数（群名/群号）：解析目标群并清空其记忆。
    非管理员一律拒绝，且不暴露命令能力细节。
    """
    target_ref = _parse_clear_group_memory_command(user_text)
    if target_ref is None:
        return None
    if not access.is_admin_user(getattr(event, "user_id", "")):
        return "该请求不可用。"

    # 无参数：群聊直接清当前群；私聊给出可清理列表。
    if not target_ref:
        if isinstance(event, GroupMessageEvent):
            group_id = int(event.group_id)
            existed, count = _clear_group_memory_namespace(group_id)
            if existed:
                return f"已清空当前群的记忆（删除 {count} 个记忆本）。"
            return "当前群没有可清理的记忆（记忆目录为空或从未生成）。"
        return await _clear_group_memory_list_text(bot)

    # 带参数：解析目标群（复用主动发送的目标解析，支持群名/别名/显式群号）。
    target, error = await _resolve_proactive_target(bot, event, "group", target_ref)
    if target is None:
        # 附带可清理群列表，方便管理员纠正群名。
        list_hint = await _clear_group_memory_list_text(bot)
        return (error or "目标群解析失败。") + "\n\n" + list_hint
    existed, count = _clear_group_memory_namespace(int(target.target_id))
    label = profiles.sanitize_name(target.label, max_len=40)
    if existed:
        return f"已清空群「{label}」的记忆（删除 {count} 个记忆本）。"
    return f"群「{label}」没有可清理的记忆（记忆目录为空或从未生成）。"
