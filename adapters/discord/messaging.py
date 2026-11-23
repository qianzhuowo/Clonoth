"""Discord attachment, sending, progress, and restart helpers.

[2026-05-14 refactor note] This module was split out of the main entry point so
message output and file handling share DiscordRuntime explicitly. The functions
avoid importing context.py to keep the requested one-way import structure.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import discord  # type: ignore[import-untyped]
import httpx


_CST = datetime.timezone(datetime.timedelta(hours=8))
logger = logging.getLogger("discord_bot")


_IMAGE_EXT_MAP: dict[str, str] = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".jfif": "image/jpeg", ".tiff": "image/tiff",
    ".tif": "image/tiff", ".ico": "image/x-icon", ".avif": "image/avif",
}

_TEXT_EXT_MAP: dict[str, str] = {
    ".txt": "text/plain", ".md": "text/markdown", ".py": "text/x-python",
    ".js": "text/javascript", ".ts": "text/typescript", ".json": "application/json",
    ".yaml": "text/yaml", ".yml": "text/yaml", ".xml": "text/xml",
    ".html": "text/html", ".css": "text/css", ".csv": "text/csv",
    ".log": "text/plain", ".sh": "text/x-shellscript", ".toml": "text/toml",
    ".ini": "text/plain", ".cfg": "text/plain", ".conf": "text/plain",
    ".c": "text/x-c", ".h": "text/x-c", ".cpp": "text/x-c++",
    ".java": "text/x-java", ".go": "text/x-go", ".rs": "text/x-rust",
    ".rb": "text/x-ruby", ".sql": "text/x-sql",
}

_AUDIO_EXT_MAP: dict[str, str] = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".webm": "audio/webm", ".flac": "audio/flac", ".amr": "audio/amr",
    ".silk": "audio/silk",
}
_AUDIO_SUFFIXES = set(_AUDIO_EXT_MAP)
_TTS_SEGMENT_RE = re.compile(
    r"<tts\b(?P<attrs>[^>]*)>(?P<html_text>[\s\S]*?)</tts>"
    r"|\[\[tts\]\](?P<legacy_text>[\s\S]*?)\[\[/tts\]\]",
    re.IGNORECASE,
)
_TTS_ATTR_RE = re.compile(
    r"(?P<name>[A-Za-z_][\w-]*)\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))"
)
_TTS_BACKENDS = {"mimo", "ryza_onnx"}
_TTS_PRESETS = {"normal_cn", "normal_ja", "whisper_cn", "whisper_ja"}


def _extract_reactions(rt: Any, text: str) -> tuple[str, list[str]]:
    """从文本中提取 [REACT:emoji] 标记，返回 (剥离后文本, emoji列表)。"""
    reactions = rt.react_pattern.findall(text)
    cleaned = rt.react_pattern.sub('', text).strip()
    return cleaned, reactions


def _resolve_image_mime(att_content_type: str | None, filename: str) -> str | None:
    if att_content_type and att_content_type.startswith("image/"):
        return att_content_type
    ext = Path(filename).suffix.lower() if filename else ""
    return _IMAGE_EXT_MAP.get(ext)


def _resolve_text_mime(att_content_type: str | None, filename: str) -> str | None:
    if att_content_type and (att_content_type.startswith("text/") or att_content_type == "application/json"):
        return att_content_type
    ext = Path(filename).suffix.lower() if filename else ""
    return _TEXT_EXT_MAP.get(ext)


def _resolve_audio_mime(att_content_type: str | None, filename: str) -> str | None:
    if att_content_type and att_content_type.startswith("audio/"):
        return att_content_type
    ext = Path(filename).suffix.lower() if filename else ""
    return _AUDIO_EXT_MAP.get(ext)


async def _collect_attachments(
    rt: Any,
    message: discord.Message,
    conversation_key: str,
    *,
    extra_messages: list[discord.Message] | None = None,
) -> list[dict[str, Any]]:
    """Save attachments from the current message and its ordered context sources."""
    result: list[dict[str, Any]] = []
    candidates = [*(extra_messages or ()), message]
    sources: list[discord.Message] = []
    seen_messages: set[int] = set()
    for src in candidates:
        source_id = src.id
        if source_id not in seen_messages:
            seen_messages.add(source_id)
            sources.append(src)

    all_atts = []
    seen_attachments: set[int] = set()
    for src in sources:
        source_atts = list(src.attachments)
        snaps = getattr(src, "message_snapshots", None)
        if snaps:
            snap = snaps[0]
            snap_msg = getattr(snap, "message", snap)
            source_atts.extend(getattr(snap_msg, "attachments", None) or getattr(snap, "attachments", None) or [])
        for att in source_atts:
            attachment_id = att.id
            if attachment_id not in seen_attachments:
                seen_attachments.add(attachment_id)
                all_atts.append(att)

    logger.info("_collect_attachments: conv_key=%s, message.attachments=%d, all_atts=%d",
                conversation_key, len(message.attachments), len(all_atts))
    for i, att in enumerate(all_atts):
        logger.info("  att[%d]: filename=%s content_type=%s size=%s",
                    i, getattr(att, 'filename', '?'), getattr(att, 'content_type', '?'), getattr(att, 'size', '?'))
        mime = _resolve_image_mime(att.content_type, att.filename or "")
        att_type = "image"
        if not mime:
            mime = _resolve_audio_mime(att.content_type, att.filename or "")
            att_type = "file"
        if not mime:
            mime = _resolve_text_mime(att.content_type, att.filename or "")
            att_type = "file"
        if not mime:
            continue
        try:
            file_bytes = await att.read()
            fallback_ext = ".png" if att_type == "image" else (Path(att.filename or "").suffix or ".bin")
            filename = att.filename or f"{os.urandom(16).hex()}{fallback_ext}"
            if rt.clonoth_client:
                # Upload via Supervisor API (Docker-safe)
                uploaded = await rt.clonoth_client.upload_attachment(
                    file_bytes, filename,
                    conversation_key=conversation_key,
                    content_type=mime,
                )
                att_dict = {
                    "type": uploaded.get("type", att_type),
                    "path": uploaded["path"],
                    "mime_type": uploaded.get("mime_type", mime),
                    "name": uploaded.get("name", filename),
                }
            else:
                # Fallback: direct write (SDK not yet initialized)
                att_dir = rt.workspace / "data" / "attachments" / conversation_key.replace(":", "_")
                att_dir.mkdir(parents=True, exist_ok=True)
                ext = Path(filename).suffix.lower() or (".png" if att_type == "image" else ".bin")
                fname = f"{os.urandom(16).hex()}{ext}"
                fpath = att_dir / fname
                fpath.write_bytes(file_bytes)
                rel_path = fpath.relative_to(rt.workspace).as_posix()
                att_dict = {
                    "type": att_type, "path": rel_path,
                    "mime_type": mime, "name": filename,
                }
            result.append(att_dict)
            logger.info("  att[%d]: SAVED -> %s (%d bytes)", i, att_dict["path"], len(file_bytes))
        except Exception as e:
            logger.warning("  att[%d]: FAILED to read/save %s: %s", i, getattr(att, 'filename', '?'), e)
    logger.info("_collect_attachments: returning %d items", len(result))
    return result


def _current_message_media_text(message: discord.Message) -> str:
    """Return a compact text marker for non-text Discord messages that are still valid input."""
    parts: list[str] = []
    if getattr(message, "stickers", None):
        names = ", ".join(s.name for s in message.stickers)
        parts.append(f"[贴纸: {names}]" if names else f"[贴纸×{len(message.stickers)}]")
    atts = list(getattr(message, "attachments", []) or [])
    if atts:
        img_count = sum(
            1 for a in atts
            if _resolve_image_mime(getattr(a, "content_type", None), getattr(a, "filename", "") or "")
        )
        audio_count = sum(
            1 for a in atts
            if _resolve_audio_mime(getattr(a, "content_type", None), getattr(a, "filename", "") or "")
        )
        other_count = max(0, len(atts) - img_count - audio_count)
        if img_count:
            parts.append(f"[图片×{img_count}]")
        if audio_count:
            parts.append(f"[语音×{audio_count}]")
        if other_count:
            parts.append(f"[附件×{other_count}]")
    if getattr(message, "embeds", None):
        parts.append(f"[嵌入内容×{len(message.embeds)}]")
    return " ".join(parts).strip()


_OUTBOUND_MENTION_OR_CODE_RE = re.compile(r"```[\s\S]*?```|``[^\r\n]+?``|`[^`\r\n]+`|<@!?(?!&)([^>\r\n]+)>")
_BRACKET_IDENTITY_RE = re.compile(r"^(.+?)\[([^\[\]]+)\]$")


def _sanitize_lookup_name(name: str, max_len: int = 32) -> str:
    """Match the display-name format emitted by Discord context."""
    name = name.replace("\n", " ").replace("\r", " ")
    name = name.replace("[", "(").replace("]", ")").strip()
    return name[:max_len] + "…" if len(name) > max_len else name


def _iter_candidate_members(rt: Any, channel: Any) -> list[Any]:
    """Prefer recent authors, then cached guild members, without duplicates."""
    candidates: list[Any] = []
    seen_ids: set[int] = set()
    for entry in reversed(rt.channel_history.get(channel.id, [])):
        message = entry["message"]
        if message is None:
            continue
        author = message.author
        if author.id not in seen_ids:
            seen_ids.add(author.id)
            candidates.append(author)
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        for member in channel.guild.members:
            if member.id not in seen_ids:
                seen_ids.add(member.id)
                candidates.append(member)
    return candidates


def _match_member_id(candidates: list[Any], raw_target: str) -> int | None:
    """Match account names before display names, including context identities."""
    target = raw_target.strip().lstrip("@").strip()
    bracket = _BRACKET_IDENTITY_RE.match(target)
    if bracket:
        uid = _match_member_id(candidates, bracket.group(2))
        if uid is not None:
            return uid
        return _match_member_id(candidates, bracket.group(1))
    target_lower = target.lower()
    for member in candidates:
        if target_lower in (member.name.lower(), _sanitize_lookup_name(member.name).lower()):
            return member.id
    for member in candidates:
        names = (member.display_name, member.global_name)
        if isinstance(member, discord.Member):
            names += (member.nick,)
        for name in names:
            if name and target_lower in (name.lower(), _sanitize_lookup_name(name).lower()):
                return member.id
    return None


def _resolve_outbound_mentions(rt: Any, channel: Any, text: str) -> str:
    """Resolve named mentions at delivery only, leaving code and history intact."""
    if "<@" not in text:
        return text
    candidates: list[Any] | None = None

    def replace(match: re.Match[str]) -> str:
        nonlocal candidates
        target = match.group(1)
        if target is None:
            return match.group(0)
        target = target.strip()
        if target.isdigit():
            return match.group(0)
        if target.lower() in ("everyone", "here"):
            return f"@{target}"
        if candidates is None:
            candidates = _iter_candidate_members(rt, channel)
        uid = _match_member_id(candidates, target)
        if uid is not None:
            return f"<@{uid}>"
        fallback = _BRACKET_IDENTITY_RE.sub(r"\1", target.lstrip("@"))
        return f"@{fallback}"

    return _OUTBOUND_MENTION_OR_CODE_RE.sub(replace, text)


def _truncate(text: str, limit: int = 2000) -> str:
    if len(text) > limit:
        return text[:limit - 4] + "..."
    return text


def _split_long_text(text: str, limit: int = 2000) -> list[str]:
    """把超长文本按段落/句子边界切成多条消息，避免直接截断丢内容。

    [2026-10-06 fix] Why: 模型忘写 [SPLIT] 标记时，长回复会被 _truncate 硬砍掉
    后半段，Discord 端只能看到带 "..." 的半截内容。How: 优先按空行、再按单换行、
    再按中文句末标点、最后按空格寻找切点，逐段切分。Purpose: 长回复完整送达，
    不再依赖模型主动加标记。
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            for punct in ("。", "！", "？", "；", "…", ".", "!", "?"):
                pos = window.rfind(punct)
                if pos > cut:
                    cut = pos + 1
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _send_split_text(
    rt: Any,
    channel: Any,
    text: str,
    *,
    reply_to: discord.Message | None = None,
    files: list[discord.File] | None = None,
) -> list[discord.Message]:
    """发送文本到频道，支持 [SPLIT] 分段和 2000 字符截断。"""
    sent: list[discord.Message] = []
    text = _resolve_outbound_mentions(rt, channel, text)
    if rt.split_signal in text:
        parts = [p.strip() for p in text.split(rt.split_signal) if p.strip()]
        for i, part in enumerate(parts):
            part = _truncate(part)
            split_files = files if (i == 0 and files) else None
            if i == 0 and reply_to:
                sent.append(await reply_to.reply(content=part, files=split_files or [], suppress_embeds=True))
            else:
                sent.append(await channel.send(content=part, files=split_files or [], suppress_embeds=True))
            if i < len(parts) - 1:
                next_len = len(parts[i + 1])
                delay = min(0.8 + next_len / 80, 3.5)
                try:
                    async with channel.typing():
                        await asyncio.sleep(delay)
                except Exception:
                    await asyncio.sleep(delay)
    else:
        # [2026-10-06 fix] 无 [SPLIT] 标记时按长度自动分段，避免超长回复被截断丢内容。
        chunks = _split_long_text(text)
        for i, chunk in enumerate(chunks):
            if len(chunk) > 2000:
                chunk = _truncate(chunk)
            chunk_files = files if (i == 0 and files) else None
            if i == 0 and reply_to:
                sent.append(await reply_to.reply(content=chunk, files=chunk_files or [], suppress_embeds=True))
            else:
                sent.append(await channel.send(content=chunk, files=chunk_files or [], suppress_embeds=True))
            if i < len(chunks) - 1:
                delay = min(0.8 + len(chunks[i + 1]) / 80, 3.5)
                try:
                    async with channel.typing():
                        await asyncio.sleep(delay)
                except Exception:
                    await asyncio.sleep(delay)
    return sent


def _record_bot_reply(
    rt: Any, channel_id: int, text: str, *,
    msg_id: int | None = None, context_watermark: int | None = None,
) -> None:
    """记录已落地回复；仅推进该回复携带的历史快照，不截断后续并发消息。"""
    if context_watermark is not None:
        watermarks = rt.session_state.last_ctx_seq
        watermarks[channel_id] = max(watermarks.get(channel_id, -1), context_watermark)
    if not text:
        return
    ts = datetime.datetime.now(_CST).strftime("%H:%M")
    # [2026-05-14 refactor note] This mirrors context._push_history locally to
    # keep messaging.py independent from context.py, as required by the split.
    q = rt.channel_history.setdefault(channel_id, [])
    record_text = f"[{ts}] Bot: {text}"
    if len(record_text) > 500:
        record_text = record_text[:497] + "..."
    q.append({
        "text": record_text,
        "message": None,
        "msg_id": msg_id,
        "reactions": {},
        "seq": rt.history_seq_counter,
        "is_bot": True,
    })
    rt.history_seq_counter += 1
    if len(q) > rt.history_max_len:
        q.pop(0)


def _resolve_attachment_path(rt: Any, attachment: dict[str, Any]) -> Path | None:
    """把 Clonoth 附件描述解析为本地路径。"""
    raw_path = attachment.get("original_path") or attachment.get("path") or attachment.get("file")
    if not raw_path:
        return None
    raw_text = str(raw_path)
    if raw_text.startswith("file://"):
        raw_text = raw_text[7:]
    path = Path(raw_text)
    return path if path.is_absolute() else rt.workspace / path


def _atts_to_discord_files(rt: Any, attachments: list[dict[str, Any]]) -> list[discord.File]:
    """将 Clonoth 附件列表转换为 discord.File 列表。"""
    files: list[discord.File] = []
    for att in attachments:
        att_path = _resolve_attachment_path(rt, att)
        if att_path and att_path.exists():
            files.append(discord.File(str(att_path), filename=att.get("name", att_path.name)))
    return files


def _is_audio_attachment(attachment: dict[str, Any]) -> bool:
    """判断附件是否为 Discord 音频附件。"""
    mime_type = str(attachment.get("mime_type") or "").lower()
    name = str(attachment.get("name") or attachment.get("filename") or "").lower()
    path = str(attachment.get("path") or "").lower()
    return mime_type.startswith("audio/") or Path(name).suffix.lower() in _AUDIO_SUFFIXES or Path(path).suffix.lower() in _AUDIO_SUFFIXES


def _delete_audio_file_quietly(rt: Any, path: Path | None) -> None:
    """按配置删除临时音频文件。失败只记日志，不影响主流程。"""
    if not path or not getattr(rt, "discord_delete_audio_after_use", True):
        return
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except Exception as exc:
        logger.warning("delete Discord temporary audio failed: path=%s error=%s", path, exc)


async def _transcribe_discord_audio_attachments(
    rt: Any,
    attachments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """把 Discord 音频附件转写为文本，并从入站附件中移除已转写音频。"""
    if not attachments or not getattr(rt, "discord_voice_input", True):
        return attachments, [], False
    if not any(_is_audio_attachment(attachment) for attachment in attachments):
        return list(attachments), [], False

    kept: list[dict[str, Any]] = []
    transcripts: list[str] = []
    had_audio = False

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        for attachment in attachments:
            if not _is_audio_attachment(attachment):
                kept.append(attachment)
                continue

            had_audio = True
            path = _resolve_attachment_path(rt, attachment)
            try:
                if path is None or not path.exists():
                    transcripts.append("（语音文件不存在，无法识别。）")
                    continue
                content = path.read_bytes()
                if not content:
                    transcripts.append("（语音文件为空，无法识别。）")
                    continue
                filename = str(attachment.get("name") or path.name)
                mime_type = str(attachment.get("mime_type") or "application/octet-stream")
                files = {"file": (filename, content, mime_type)}
                response = await client.post(f"{rt.base_url.rstrip('/')}/v1/audio/transcribe", files=files, headers={"Authorization": f"Bearer {rt.clonoth_client._resolve_admin_token()}"})
                response.raise_for_status()
                payload = response.json()
                text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
                transcripts.append(text or "（语音识别没有返回文字。）")
            except Exception as exc:
                logger.warning("Discord voice transcription failed: attachment=%s error=%s", attachment, exc)
                transcripts.append(f"（语音识别失败：{exc}）")
            finally:
                _delete_audio_file_quietly(rt, path)

    return kept, transcripts, had_audio


def _strip_voice_placeholders(text: str) -> str:
    """清理媒体占位符，避免和语音转写内容重复。"""
    out = re.sub(r"\s*\[语音(?:×\d+)?\]\s*", " ", text or "")
    return re.sub(r"\s+", " ", out).strip()


def _compose_discord_user_text(raw_text: str, transcripts: list[str], had_audio: bool) -> tuple[str, str, str]:
    """生成 Discord 入站文本、历史文本和输入类型。"""
    clean_text = _strip_voice_placeholders(raw_text)
    transcript_text = "\n".join(t for t in (s.strip() for s in transcripts) if t).strip()
    if had_audio and clean_text and transcript_text:
        base = f"【输入方式】Discord 文字加语音消息\n【用户文字】\n{clean_text}\n\n【语音转写】\n{transcript_text}"
        history = f"{clean_text} [语音转写] {transcript_text}"
        return base, history, "voice"
    if had_audio and transcript_text:
        base = f"【输入方式】Discord 语音消息\n【语音转写】\n{transcript_text}"
        history = f"[语音转写] {transcript_text}"
        return base, history, "voice"
    if had_audio:
        base = "【输入方式】Discord 语音消息\n【语音转写】\n（语音未能识别出可用文本。）"
        return base, "[语音未能识别出可用文本]", "voice"
    base = clean_text or raw_text.strip() or "你好"
    return base, base, "text"


def append_discord_reply_modality_hint(rt: Any, inbound_text: str, input_modality: str, is_dm: bool) -> str:
    """按运行配置给模型加入 Discord TTS 协议提示。"""
    if not getattr(rt, "discord_voice_reply", True):
        return inbound_text
    protocol = (
        "Discord 适配器已接入语音合成；这不是需要调用或搜索的模型工具。"
        "需要语音时输出带明确参数的标签。使用本地参考音频时，格式为 "
        "<tts backend=\"ryza_onnx\" preset=\"normal_cn\">正文</tts>；preset 可选 "
        "normal_cn、normal_ja、whisper_cn、whisper_ja，应按语言和说话情景选择。"
        "使用 Mimo 时，格式为 <tts backend=\"mimo\" voice=\"音色名\">正文</tts>，voice 可省略以使用配置的默认音色。"
        "每个 TTS 段都可以独立选择；适配器会原样传给合成后端，不会用语言检测覆盖明确选择。"
        "正常文字必须包含完整回复，TTS 只能作为附加语音；不要发送纯 TTS。"
    )
    if input_modality == "voice" and getattr(rt, "discord_voice_reply_on_voice_input", True):
        hint = (
            "【Discord 回复方式提示】\n"
            f"{protocol}本轮用户通过 Discord 语音消息发来请求，请优先附加简短、自然的语音版本。"
        )
    elif input_modality == "text":
        hint = (
            "【Discord 回复方式提示】\n"
            f"{protocol}本轮用户通过 Discord 文字消息发来请求。只有用户明确要求语音，"
            "或确实适合附加语音时，才使用 TTS 标记；否则不要使用。"
        )
    else:
        return inbound_text
    if not is_dm:
        hint += " 群聊中应保持克制，只有确实适合时才标出语音内容。"
    marker = "请根据以上上下文，执行当前用户的指令并给出回复。"
    if marker in inbound_text:
        return inbound_text.replace(marker, f"{hint}\n\n{marker}")
    return f"{inbound_text}\n\n{hint}"


def _parse_tts_attributes(raw: str) -> dict[str, str]:
    """Parse the small, allow-listed attribute surface accepted by ``<tts>``."""
    attributes: dict[str, str] = {}
    for match in _TTS_ATTR_RE.finditer(raw or ""):
        name = match.group("name").lower()
        value = next((item for item in (match.group("double"), match.group("single"), match.group("bare")) if item is not None), "").strip()
        if name in {"backend", "preset", "voice", "instruction"} and value:
            attributes[name] = value
    backend = attributes.get("backend", "").lower()
    if backend and backend not in _TTS_BACKENDS:
        attributes.pop("backend", None)
    preset = attributes.get("preset", "").lower()
    if preset:
        if preset in _TTS_PRESETS:
            attributes["preset"] = preset
        else:
            attributes.pop("preset", None)
    return attributes


def _extract_tts_segments(text: str) -> tuple[str, list[dict[str, str]]]:
    """提取 TTS 段及其逐段合成参数，并返回去掉标记后的展示文本。"""
    segments: list[dict[str, str]] = []

    def _replace(match: re.Match[str]) -> str:
        segment = (match.group("html_text") or match.group("legacy_text") or "").strip()
        if segment:
            item = {"text": segment}
            item.update(_parse_tts_attributes(match.group("attrs") or ""))
            segments.append(item)
        return ""

    display_text = _TTS_SEGMENT_RE.sub(_replace, text or "")
    display_text = re.sub(r"[ \t]{2,}", " ", display_text)
    display_text = re.sub(r"[ \t]+\n", "\n", display_text)
    display_text = re.sub(r"\n[ \t]+", "\n", display_text)
    display_text = re.sub(r"\n{3,}", "\n\n", display_text).strip()
    return display_text, segments


def _fallback_spoken_text(display_text: str) -> str:
    """语音输入需要回复但模型没有显式标记时，取一段短文本合成。"""
    text = re.sub(r"```[\s\S]*?```", "", display_text or "")
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 600:
        text = text[:600].rstrip() + "。"
    return text


def _discord_voice_reply_allowed(rt: Any, input_modality: str, has_tts_segments: bool) -> bool:
    """根据配置和输入方式判断是否发送 Discord 语音回复。"""
    if not getattr(rt, "discord_voice_reply", True):
        return False
    if input_modality == "voice" and getattr(rt, "discord_voice_reply_on_voice_input", True):
        return True
    return bool(has_tts_segments)


async def _synthesize_discord_tts(
    rt: Any,
    segment: dict[str, str],
    conversation_key: str,
    client: httpx.AsyncClient,
) -> Path | None:
    """调用 Supervisor 的统一 TTS 接口，并保留模型为该段指定的合成参数。"""
    spoken = (segment.get("text") or "").strip()
    if not spoken:
        return None
    body: dict[str, Any] = {
        "text": spoken,
        "conversation_key": conversation_key or "discord_audio",
        "format": getattr(rt, "discord_tts_format", "wav") or "wav",
    }
    # Segment parameters are authoritative. Runtime values are only fallbacks.
    backend = (segment.get("backend") or getattr(rt, "discord_tts_backend", "") or "").strip().lower()
    preset = (segment.get("preset") or "").strip().lower()
    voice = (segment.get("voice") or getattr(rt, "discord_tts_voice", "") or "").strip()
    instruction = (segment.get("instruction") or getattr(rt, "discord_tts_instruction", "") or "").strip()
    if backend in _TTS_BACKENDS:
        body["backend"] = backend
    if preset in _TTS_PRESETS:
        body["preset"] = preset
    if voice:
        body["voice"] = voice
    if instruction:
        body["instruction"] = instruction
    try:
        response = await client.post(f"{rt.base_url.rstrip('/')}/v1/audio/synthesize", json=body, headers={"Authorization": f"Bearer {rt.clonoth_client._resolve_admin_token()}"})
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("Discord TTS synthesize failed: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    attachment = payload.get("attachment")
    if not isinstance(attachment, dict):
        return None
    return _resolve_attachment_path(rt, attachment)


async def _send_discord_tts_segments(rt: Any, channel: Any, conversation_key: str, segments: list[dict[str, str]]) -> bool:
    """逐段合成并发送 Discord 语音附件，返回是否成功落地。"""
    if not segments:
        return False
    sent = False
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        for index, segment in enumerate(segments, start=1):
            path: Path | None = None
            try:
                path = await _synthesize_discord_tts(rt, segment, conversation_key, client)
                if path is None or not path.exists():
                    continue
                await channel.send(file=discord.File(str(path), filename=path.name), suppress_embeds=True)
                sent = True
            except Exception as exc:
                logger.warning("send Discord TTS audio failed: segment=%s error=%s", index, exc)
            finally:
                _delete_audio_file_quietly(rt, path)
    return sent


def prepare_discord_tts_reply(rt: Any, text: str, input_modality: str) -> tuple[str, list[dict[str, str]]]:
    """准备展示文本和需要发送的 TTS 段。"""
    display_text, tts_segments = _extract_tts_segments(text or "")
    should_send_voice = _discord_voice_reply_allowed(rt, input_modality, bool(tts_segments))
    voice_segments = list(tts_segments)
    if should_send_voice and not voice_segments:
        fallback = _fallback_spoken_text(display_text)
        if fallback:
            voice_segments = [{"text": fallback}]
    return display_text, voice_segments if should_send_voice else []


def _format_progress_log(
    prefix: str,
    lines: list[str],
    *,
    dot_state: dict[str, Any] | None = None,
    status: str = "",
    max_lines: int = 6,
) -> str:
    """统一进度日志渲染。主节点和子节点共用。"""
    if not lines and not dot_state:
        return ""

    log_block = ""
    if lines:
        display = lines[-max_lines:]
        start_idx = max(0, len(lines) - max_lines) + 1
        numbered = [f"{start_idx + i}| {l}" for i, l in enumerate(display)]
        log_block = "```\n" + "\n".join(numbered) + "\n```"

    anim_line = ""
    preview_block = ""
    if dot_state:
        if dot_state.get("had_stream_activity"):
            dot_state["dot_step"] = dot_state.get("dot_step", 0) + 1
            dot_state["had_stream_activity"] = False
        step = dot_state.get("dot_step", 0)
        phase = step % 10
        n_dots = phase + 1 if phase <= 5 else 11 - phase
        anim_line = "•" * n_dots

        start_t = dot_state.get("thinking_start_time", 0)
        if start_t > 0:
            elapsed = int(time.time() - start_t)
            anim_line += f" ⏱ {elapsed}s"

        retry_info = dot_state.get("retry_info", "")
        if retry_info:
            anim_line += f"\n{retry_info}"
            if dot_state.get("had_stream_activity"):
                dot_state["retry_info"] = ""

        # --- stream preview（thinking / text）---
        thinking_pv = dot_state.get("thinking_preview", "")
        text_pv = dot_state.get("text_preview", "")
        preview = thinking_pv or text_pv
        if preview:
            if "<<<TOOL_CALL>>>" in preview or "<<<END_TOOL_CALL>>>" in preview:
                preview = ""
        if preview:
            p_lines = preview.strip().split("\n")
            p_lines = [l for l in p_lines if l.strip()][-2:]
            if p_lines:
                p_lines = [l[:80].replace("`", "'") for l in p_lines]
                preview_block = "```\n" + "\n".join(p_lines) + "\n```"

    header = f"⏳ {prefix}:"
    parts = [header]
    if anim_line:
        parts.append(anim_line)
    if preview_block:
        parts.append(preview_block)
    if log_block:
        parts.append(log_block)
    if status:
        parts.append(status)

    text = "\n".join(parts)
    if len(text) > 2000:
        text = text[-2000:]
    return text


def _format_status_summary(dot_state: dict[str, Any] | None) -> str:
    """渲染触发频道的处理中消息：工具摘要 + free prose，不含 stream preview。"""
    if not dot_state:
        return ""
    prose_lines = dot_state.get("prose_lines") or []
    tool_lines = dot_state.get("tool_lines") or []
    parts: list[str] = []
    for pl in prose_lines[-3:]:
        parts.append(pl.replace("`", "'")[:80])
    for tl in tool_lines[-5:]:
        mark = "✓" if tl.get("done") and tl.get("ok") else "✗" if tl.get("done") else "…"
        parts.append(f"{tl.get('text', '')} {mark}")
    if not parts:
        # 没有活动时显示计时器
        start_t = dot_state.get("thinking_start_time", 0)
        if start_t > 0:
            elapsed = int(time.time() - start_t)
            return f"⏳ 处理中... ⏱ {elapsed}s"
        return "⏳ 处理中..."
    header = "⏳ 处理中:"
    text = header + "\n" + "\n".join(parts)
    if len(text) > 2000:
        text = text[-2000:]
    return text


async def _safe_restart(rt: Any, channel_id: int = 0, delay: float = 3.0) -> None:
    """延迟后通过 pm2 restart 安全重启自身。"""
    if channel_id:
        try:
            notify_path = Path(f"/tmp/clonoth_restart_notify_{rt.bridge_port}.json")
            notify_path.write_text(json.dumps({"channel_id": channel_id}))
        except Exception:
            pass
    await asyncio.sleep(delay)
    if rt.session_state:
        for _rs_seq, _rs_trigger in list(rt.session_state.triggers.items()):
            try:
                status_msg = _rs_trigger.platform_data.get("status_msg")
                if status_msg:
                    await status_msg.edit(content="⚠️ Bot 正在重启...", view=None)
            except Exception:
                pass
        # [2026-05-14 bug fix] The original function referenced self outside a
        # class. It now reaches the callback helper through DiscordRuntime.
        callback_obj = rt.callbacks
        if callback_obj:
            for _rs_seq, _rs_state in list(rt.session_state.main_task_states.items()):
                await callback_obj._settle_log_msg(_rs_state, "⚠️ Bot 重启中")
            for _rs_key, _rs_child in list(rt.session_state.child_task_states.items()):
                if getattr(_rs_child, "is_dm", False):
                    msg = _rs_child.platform_data.get("msg")
                    if msg:
                        await msg.delete()
                else:
                    await callback_obj._settle_log_msg(_rs_child, "⚠️ Bot 重启中")
        rt.session_state.triggers.clear()
        rt.session_state.main_task_states.clear()
        rt.session_state.child_task_states.clear()
        rt.session_state.conversation_sessions.clear()
        rt.session_state.session_conv_map.clear()
    print("[bot] 收到安全重启信号，正在退出（PM2 将自动重启）...")
    sys.exit(0)
