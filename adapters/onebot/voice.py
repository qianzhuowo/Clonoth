"""QQ voice: speech-to-text for inbound record messages and TTS voice replies.

Inbound: record segments -> mp3 (NapCat get_record) or raw url/path bytes ->
Supervisor ``/v1/audio/transcribe``. Audio is posted from memory and never
stored under data/attachments.

Outbound: the model marks speakable text with ``<tts>...</tts>`` or
``[[tts]]...[[/tts]]``; marked segments are synthesized via Supervisor
``/v1/audio/synthesize`` and sent as QQ voice after the text reply.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from . import config, state
from .emoji_handler import strip_output_markers

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".aac", ".amr", ".silk", ".webm"}
TTS_SEGMENT_RE = re.compile(r"<tts>([\s\S]*?)</tts>|\[\[tts\]\]([\s\S]*?)\[\[/tts\]\]", re.IGNORECASE)
_QQ_EMOJI_MARK_RE = re.compile(r"\[QQ_EMOJI:(.+?)\]")
REPLY_ANCHOR = "请根据以上上下文，执行当前用户的指令并给出回复。"
_AUDIO_TIMEOUT = 120.0


def guess_audio_mime(source: str, content_type: str = "") -> str:
    probe = f"{source or ''} {content_type or ''}".lower()
    for needle, mime in (("wav", "audio/wav"), ("ogg", "audio/ogg"), ("opus", "audio/ogg"), ("m4a", "audio/aac"),
                         ("aac", "audio/aac"), ("amr", "audio/amr"), ("webm", "audio/webm"), ("silk", "audio/silk")):
        if needle in probe:
            return mime
    return "audio/mpeg"


def iter_record_items(message: Any) -> List[Dict[str, Any]]:
    """Data dicts of OneBot ``record`` segments."""
    try:
        segments = list(message or [])
    except Exception:
        return []
    return [dict(getattr(s, "data", {}) or {}) for s in segments
            if getattr(s, "type", "") == "record" and getattr(s, "data", None)]


def _auth_headers() -> Dict[str, str]:
    token = state._client._resolve_admin_token() if state._client is not None else ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _local_path(raw: str) -> Optional[Path]:
    raw = (raw or "").strip()
    if not raw or raw.startswith(("http://", "https://")):
        return None
    path = Path(raw[7:] if raw.startswith("file://") else raw)
    return path if path.exists() else None


async def _record_bytes(bot: Any, record: Dict[str, Any], client: httpx.AsyncClient) -> Tuple[bytes, str, str]:
    """(content, filename, mime). NapCat mp3 conversion first: the ASR cannot read SILK."""
    file_id = str(record.get("file") or "").strip()
    if file_id and bot is not None:
        try:
            payload = await bot.call_api("get_record", file=file_id, out_format="mp3")
            if isinstance(payload, dict):
                path = _local_path(str(payload.get("file") or payload.get("path") or payload.get("url") or ""))
                if path is not None:
                    return path.read_bytes(), "voice.mp3", "audio/mpeg"
        except Exception as exc:
            logger.warning("NapCat get_record mp3 failed: file=%s error=%s", file_id, exc)
    url = str(record.get("url") or "").strip()
    if url:
        response = await client.get(url)
        response.raise_for_status()
        mime = guess_audio_mime(url, response.headers.get("content-type", ""))
        return response.content, "voice", mime
    path = _local_path(str(record.get("path") or ""))
    if path is not None:
        return path.read_bytes(), path.name, guess_audio_mime(path.name)
    return b"", "", ""


async def transcribe_message(bot: Any, message: Any) -> Tuple[List[str], bool]:
    """Transcribe record segments; returns (transcripts, had_audio)."""
    records = iter_record_items(message)
    if not records or not config.ONEBOT_VOICE_INPUT:
        return [], False
    transcripts: List[str] = []
    url = f"{config.CLONOTH_BASE_URL.rstrip('/')}/v1/audio/transcribe"
    async with httpx.AsyncClient(timeout=_AUDIO_TIMEOUT, follow_redirects=True) as client:
        for record in records:
            try:
                content, filename, mime = await _record_bytes(bot, record, client)
                if not content:
                    transcripts.append("（语音文件为空，无法识别。）")
                    continue
                response = await client.post(url, files={"file": (filename, content, mime)}, headers=_auth_headers())
                response.raise_for_status()
                payload = response.json()
                text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
                transcripts.append(text or "（语音识别没有返回文字。）")
            except Exception as exc:
                logger.warning("QQ voice transcription failed: record=%s error=%s", record, exc)
                transcripts.append(f"（语音识别失败：{exc}）")
    return transcripts, True


def compose_user_text(raw_text: str, transcripts: List[str], had_audio: bool) -> Tuple[str, str, str]:
    """(text for the model, text for group history, input modality)."""
    clean = re.sub(r"\s*\[语音\]\s*", " ", raw_text or "").strip()
    spoken = "\n".join(t for t in (s.strip() for s in transcripts) if t).strip()
    if had_audio and clean and spoken:
        return (f"【输入方式】QQ文字加语音消息\n【用户文字】\n{clean}\n\n【语音转写】\n{spoken}",
                f"{clean} [语音转写] {spoken}", "voice")
    if had_audio and spoken:
        return f"【输入方式】QQ语音消息\n【语音转写】\n{spoken}", f"[语音转写] {spoken}", "voice"
    if had_audio:
        return "【输入方式】QQ语音消息\n【语音转写】\n（语音未能识别出可用文本。）", "[语音未能识别出可用文本]", "voice"
    base = clean or (raw_text or "").strip() or "你好"
    return base, base, "text"


def append_reply_modality_hint(inbound_text: str, input_modality: str, is_dm: bool) -> str:
    """Tell the model when a voice reply is expected/allowed; users never type markers."""
    if not config.ONEBOT_VOICE_REPLY:
        return inbound_text
    if input_modality == "voice" and config.ONEBOT_VOICE_REPLY_ON_VOICE_INPUT:
        hint = ("【QQ回复方式提示】\n本轮用户通过 QQ 语音消息发来请求。请优先生成适合语音发送的简短自然语言回复，"
                "并用 <tts>...</tts> 标出适合发送为 QQ 语音消息的内容。普通正文仍可保留必要说明。")
    elif input_modality == "text" and config.ONEBOT_VOICE_REPLY_AUTO_ON_TEXT:
        hint = ("【QQ回复方式提示】\n本轮用户通过 QQ 文字消息发来请求。通常使用文字回复。若你判断本轮适合额外发送语音回复，"
                "例如用户明确要求语音、回复很短、需要提醒或安抚，可以用 <tts>...</tts> 标出适合发送为 QQ 语音消息的内容。"
                "如果不适合语音回复，不要使用 TTS 标记。")
    else:
        return inbound_text
    if not is_dm:
        hint += " 群聊中应保持克制，只有确实适合时才标出语音内容。"
    if REPLY_ANCHOR in inbound_text:
        return inbound_text.replace(REPLY_ANCHOR, f"{hint}\n\n{REPLY_ANCHOR}", 1)
    return f"{inbound_text}\n\n{hint}"


def extract_tts_segments(text: str) -> Tuple[str, List[str]]:
    """(display text without markers, marked segments)."""
    segments: List[str] = []

    def _replace(match: re.Match[str]) -> str:
        segment = (match.group(1) or match.group(2) or "").strip()
        if segment:
            segments.append(segment)
        return segment

    return TTS_SEGMENT_RE.sub(_replace, text or "").strip(), segments


def voice_reply_allowed(target: Dict[str, Any], input_modality: str, has_tts_segments: bool) -> bool:
    if not config.ONEBOT_VOICE_REPLY:
        return False
    if target.get("type") == "private" and not config.ONEBOT_VOICE_REPLY_PRIVATE:
        return False
    if target.get("type") == "group" and not config.ONEBOT_VOICE_REPLY_GROUP:
        return False
    if input_modality == "voice" and config.ONEBOT_VOICE_REPLY_ON_VOICE_INPUT:
        return True
    return input_modality == "text" and has_tts_segments and config.ONEBOT_VOICE_REPLY_AUTO_ON_TEXT


def fallback_spoken_text(display_text: str) -> str:
    """Speakable text when a voice reply is required but nothing was marked."""
    text = _QQ_EMOJI_MARK_RE.sub("", strip_output_markers(display_text or ""))
    text = re.sub(r"\s+", " ", re.sub(r"```[\s\S]*?```", "", text)).strip()
    return text[:600].rstrip() + "。" if len(text) > 600 else text


def voice_plan(text: str, target: Dict[str, Any], input_modality: str) -> Tuple[str, List[str]]:
    """(display text, segments to speak); segments is empty when no voice should be sent."""
    display, segments = extract_tts_segments(text)
    if not voice_reply_allowed(target, input_modality, bool(segments)):
        return display, []
    if not segments:
        fallback = fallback_spoken_text(display)
        segments = [fallback] if fallback else []
    return display, segments


def _resolve_path(attachment: Dict[str, Any]) -> Optional[Path]:
    raw = str(attachment.get("original_path") or attachment.get("path") or attachment.get("file") or "")
    if not raw:
        return None
    path = Path(raw[7:] if raw.startswith("file://") else raw)
    return path if path.is_absolute() else Path(config.CLONOTH_WORKSPACE) / path


async def synthesize(text: str, conversation_key: str) -> Optional[Path]:
    spoken = (text or "").strip()
    if not spoken:
        return None
    body: Dict[str, Any] = {"text": spoken, "conversation_key": conversation_key or "qq_audio",
                            "format": config.ONEBOT_TTS_FORMAT or "wav"}
    if config.ONEBOT_TTS_VOICE:
        body["voice"] = config.ONEBOT_TTS_VOICE
    if config.ONEBOT_TTS_INSTRUCTION:
        body["instruction"] = config.ONEBOT_TTS_INSTRUCTION
    try:
        async with httpx.AsyncClient(timeout=_AUDIO_TIMEOUT, follow_redirects=True) as client:
            response = await client.post(f"{config.CLONOTH_BASE_URL.rstrip('/')}/v1/audio/synthesize",
                                         json=body, headers=_auth_headers())
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        logger.warning("QQ TTS synthesize failed: %s", exc)
        return None
    attachment = payload.get("attachment") if isinstance(payload, dict) else None
    return _resolve_path(attachment) if isinstance(attachment, dict) else None


async def send_voice_segments(bot: Any, target: Dict[str, Any], conversation_key: str, segments: List[str]) -> None:
    """Synthesize and send each segment as QQ voice; falls back to a file upload."""
    from nonebot.adapters.onebot.v11 import MessageSegment

    from . import send

    for segment in segments:
        path = await synthesize(segment, conversation_key)
        if path is None or not path.exists():
            continue
        try:
            try:
                await send._send_qq_message(bot, target, MessageSegment.record(file=str(path.resolve())))
            except Exception as exc:
                logger.warning("send QQ record failed, fallback to file upload: path=%s error=%s", path, exc)
                await send._send_attachment_path(bot, target, path, filename=path.name)
        finally:
            if config.ONEBOT_DELETE_AUDIO_AFTER_USE:
                try:
                    path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.warning("delete temporary audio failed: path=%s error=%s", path, exc)
