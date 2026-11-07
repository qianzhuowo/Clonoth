"""QQ custom faces (NapCat favourites): prompt block, sync and admin commands.

Two files, both kept out of git:
- names file: base names the model may use as ``[表情:名称]``;
- metadata file: md5/resId/emojiId per face, used only by code.

Every write command (sync/add/rename/delete) and the list commands are
admin-only; the caller passes ``is_admin``. The web panel (plugins/qq_faces)
reaches the same operations through the loopback API in faces_api.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .emoji_handler import (
    _normalize_name as normalize_face_name,
    count_duplicate_face_names,
    custom_face_summary,
    extract_named_custom_face_metadata,
    extract_named_custom_face_names,
    fetch_custom_face_details,
    find_custom_faces_by_base_name,
    invalidate_custom_face_cache,
    list_custom_face_aliases,
    load_custom_face_metadata,
    load_custom_face_names,
    resolve_custom_face,
    write_custom_face_metadata,
    write_custom_face_names,
)
from .inbound_attachments import (
    RecentImages,
    attachment_abs_path,
    custom_face_command_images,
    image_ext_from_url_or_mime,
    sniff_image_mime,
)

logger = logging.getLogger(__name__)

LIST_RE = re.compile(r"^(?:表情列表|收藏表情列表|emoji列表|可用表情)(?:\s+(\d+))?$", re.IGNORECASE)
DETAIL_LIST_RE = re.compile(r"^(?:表情详情列表|收藏表情详情|表情管理列表)(?:\s+(\d+))?$", re.IGNORECASE)
SYNC_RE = re.compile(r"^(?:同步表情列表|刷新表情列表|更新表情列表)$", re.IGNORECASE)
HELP_RE = re.compile(r"^(?:表情包帮助|表情帮助|表情包命令|表情命令帮助)$", re.IGNORECASE)
ADD_RE = re.compile(r"^(?:收藏表情|添加表情|保存表情|表情收藏)\s+(.+?)\s*$", re.IGNORECASE)
RENAME_RE = re.compile(r"^(?:命名表情|重命名表情|改名表情)\s+(\S+)\s+(.+?)\s*$", re.IGNORECASE)
DELETE_RE = re.compile(r"^(?:删除表情|移除表情|取消收藏表情)\s+(.+?)\s*$", re.IGNORECASE)

HELP_TEXT = (
    "【表情包管理命令示例（仅管理员）】\n"
    "1) 同步表情列表\n   从 NapCat 收藏同步已命名表情到本地文件。\n"
    "2) 表情列表 / 表情列表 50\n   查看 AI 当前可用的表情名称。\n"
    "3) 表情详情列表 / 表情详情列表 50\n   查看收藏详情（含未命名项）与序号。\n"
    "4) 收藏表情 开心\n   收藏当前消息/引用/最近的一张图片，并命名为“开心”。\n"
    "5) 命名表情 3 开心 或 重命名表情 3 开心\n   给第 3 个收藏表情命名/改名（也可用 md5/resId/文件名定位）。\n"
    "6) 删除表情 开心\n   删除名为“开心”的收藏表情。\n"
    "提示：AI 发送表情用 [表情:名称]；未命名表情不会给 AI 使用。"
)
_DETAIL_UNSUPPORTED = "当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"


def face_value(face: Any, *keys: str) -> Any:
    """First present, non-empty field; keeps legal falsy values such as 0."""
    if not isinstance(face, dict):
        return None
    for key in keys:
        value = face.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return value
    return None


def _sibling_lines(siblings: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for s in siblings:
        extra = s.get("md5") or s.get("res_id") or s.get("file_name") or ""
        note = f"（md5:{str(extra)[:8]}）" if s.get("md5") else (f"（{extra}）" if extra else "")
        lines.append(f"{s['index']}. {s['base_name']}{note}")
    return lines


def _limit(match: re.Match[str], default: int) -> int:
    return max(1, min(100, int(match.group(1) or default)))


def _find_face(faces: List[Any], *, md5: str = "", res_id: str = "") -> Optional[Dict[str, Any]]:
    """Locate one live face by md5 (case-insensitive) or resId."""
    for face in faces:
        if not isinstance(face, dict):
            continue
        if md5 and str(face_value(face, "md5", "MD5") or "").lower() == md5.lower():
            return face
        if res_id and str(face_value(face, "resId", "res_id", "id") or "") == res_id:
            return face
    return None


def _face_desc(face: Dict[str, Any]) -> str:
    return str(face_value(face, "desc", "description", "name", "title", "alias") or "").strip()


class CustomFaces:
    def __init__(
        self,
        *,
        names_path: Optional[str] = None,
        metadata_path: Optional[str] = None,
        bqbs: Optional[List[str]] = None,
        prompt_limit: Optional[int] = None,
        workspace: str | Path | None = None,
        sync_settle_seconds: float = 0.8,
        verify_attempts: int = 4,
    ) -> None:
        self.names_path = names_path or config.CUSTOM_FACE_NAMES_PATH
        self.metadata_path = metadata_path or config.CUSTOM_FACE_METADATA_PATH
        self.bqbs: List[str] = list(bqbs or [])
        self.prompt_limit = config.CUSTOM_FACE_PROMPT_LIMIT if prompt_limit is None else prompt_limit
        self.workspace = workspace
        self.sync_settle_seconds = sync_settle_seconds
        self.verify_attempts = max(1, verify_attempts)
        self.metadata: List[Dict[str, Any]] = []

    # ---- files / prompt ----------------------------------------------
    def current_names(self) -> List[str]:
        """Read on every call so manual edits apply without a restart."""
        return load_custom_face_names(self.names_path)

    def current_metadata(self) -> List[Dict[str, Any]]:
        self.metadata = load_custom_face_metadata(self.metadata_path)
        return list(self.metadata)

    def _write_snapshot(self, faces: List[Any]) -> List[str]:
        """Write names + metadata files from one live favourites list."""
        self.metadata = extract_named_custom_face_metadata(faces, self.bqbs)
        names = extract_named_custom_face_names(faces, self.bqbs)
        write_custom_face_names(self.names_path, names)
        write_custom_face_metadata(self.metadata_path, self.metadata)
        return names

    async def sync(self, bot: Any) -> List[str]:
        """Pull named favourites from NapCat; names file keeps unique base names,
        metadata keeps every same-name face."""
        return self._write_snapshot(await self._live_faces(bot))

    def prompt_position(self, name: str) -> int:
        """0-based position of a base name in the AI names file; -1 when absent."""
        key = normalize_face_name(name)
        for pos, item in enumerate(self.current_names()):
            if normalize_face_name(item) == key:
                return pos
        return -1

    def usage_note(self, name: str) -> str:
        """Tell the admin whether the model can actually see this name."""
        pos = self.prompt_position(name)
        if pos < 0:
            return f"注意：AI 名称列表里还没有「{name}」，可发送“同步表情列表”刷新。"
        if pos >= self.prompt_limit:
            return (f"注意：它在 AI 名称列表第 {pos + 1} 位，超过提示上限 {self.prompt_limit}，模型看不到；"
                    "可调大 ONEBOT_CUSTOM_FACE_PROMPT_LIMIT。")
        return f"模型可用 [表情:{name}] 调用。"

    # ---- NapCat operations ---------------------------------------------
    # Every write is verified against a fresh favourites list before the
    # result is reported; NapCat may accept a call without applying it.
    async def _live_faces(self, bot: Any) -> List[Any]:
        invalidate_custom_face_cache(bot)
        return await fetch_custom_face_details(bot, force=True)

    async def _wait_until(self, bot: Any, done: Any) -> tuple[List[Any], bool]:
        """Re-read favourites until ``done(faces)``; returns (last faces, reached)."""
        faces: List[Any] = []
        for _ in range(self.verify_attempts):
            if self.sync_settle_seconds > 0:
                await asyncio.sleep(self.sync_settle_seconds)
            faces = await self._live_faces(bot)
            if done(faces):
                return faces, True
        return faces, False

    async def set_desc(self, bot: Any, face: Dict[str, Any], desc: str) -> tuple[bool, str]:
        emoji_id = face_value(face, "emojiId", "emoji_id", "emoId", "emoid")
        res_id = face_value(face, "resId", "res_id", "id")
        md5 = face_value(face, "md5", "MD5")
        missing = [n for n, v in (("emoji_id", emoji_id), ("res_id", res_id), ("md5", md5)) if v is None or v == ""]
        if missing:
            return False, "找到了该表情，但缺少重命名所需字段：" + "、".join(missing)
        desc = desc.strip()
        if not desc:
            return False, "表情名称不能为空。"
        try:
            await bot.call_api("set_custom_face_desc", emoji_id=emoji_id, res_id=str(res_id), md5=str(md5), desc=desc)
        except Exception:
            logger.warning("set_custom_face_desc failed", exc_info=True)
            return False, "重命名收藏表情失败：当前 OneBot 实现可能不支持 set_custom_face_desc，或该表情资源信息已失效。"

        def applied(faces: List[Any]) -> bool:
            live = _find_face(faces, md5=str(md5))
            return live is not None and normalize_face_name(_face_desc(live)) == normalize_face_name(desc)

        try:
            faces, ok = await self._wait_until(bot, applied)
        except Exception:
            logger.warning("re-read favourites after set_custom_face_desc failed", exc_info=True)
            return False, "已提交重命名，但重新读取收藏列表失败，无法确认结果。"
        self._write_snapshot(faces)
        if not ok:
            live = _find_face(faces, md5=str(md5))
            current = _face_desc(live) if live else ""
            return False, f"NapCat 没有应用新名称，该表情当前名称仍为「{current or '未命名'}」。"
        return True, f"已将收藏表情重命名为：{desc}\n" + self.usage_note(desc)

    async def delete_face(self, bot: Any, face: Dict[str, Any]) -> tuple[bool, str]:
        res_id = str(face_value(face, "resId", "res_id", "id") or "")
        if not res_id:
            return False, "该表情没有 resId，无法删除。"
        try:
            await bot.call_api("delete_custom_face", res_id=res_id)
        except Exception:
            logger.warning("delete_custom_face failed", exc_info=True)
            return False, "删除收藏表情失败：当前 OneBot 实现可能不支持 delete_custom_face，或 resId 已失效。"
        try:
            faces, gone = await self._wait_until(bot, lambda fs: _find_face(fs, res_id=res_id) is None)
        except Exception:
            logger.warning("re-read favourites after delete_custom_face failed", exc_info=True)
            return False, "已提交删除，但重新读取收藏列表失败，无法确认结果。"
        self._write_snapshot(faces)
        if not gone:
            return False, "NapCat 返回成功，但该表情仍在收藏列表中。"
        return True, "已删除收藏表情。"

    async def add_image_file(self, bot: Any, alias: str, path: Path) -> tuple[bool, str]:
        """Favourite an image file and name it; the reply reflects the verified result.

        An image already in favourites (same md5) is not added again: QQ keeps
        one entry per image, so the existing entry is renamed instead.
        """
        alias = (alias or "").strip()
        if not alias:
            return False, "表情名称不能为空。"
        if not path.is_file():
            return False, "找不到要收藏的图片文件，请重新发送图片后再试。"
        md5 = hashlib.md5(path.read_bytes()).hexdigest()
        try:
            faces = await self._live_faces(bot)
        except Exception:
            logger.warning("fetch favourites before add_custom_face failed", exc_info=True)
            return False, f"收藏表情失败：{_DETAIL_UNSUPPORTED}"
        existing = _find_face(faces, md5=md5)
        if existing is not None:
            previous = _face_desc(existing)
            if normalize_face_name(previous) == normalize_face_name(alias):
                self._write_snapshot(faces)
                return True, f"这张图片已在收藏中，名称就是「{alias}」。\n" + self.usage_note(alias)
            target = existing
            head = f"这张图片已在收藏中（原名「{previous or '未命名'}」），已改名为：{alias}"
        else:
            try:
                await bot.call_api("add_custom_face", file=str(path), md5=md5, file_name=path.name, is_origin=True)
            except Exception:
                logger.warning("add_custom_face failed", exc_info=True)
                return False, (
                    "收藏表情失败：NapCat add_custom_face 调用失败。\n"
                    "提示：NapCat 要求 file 是 NapCat 运行环境可访问的本地路径；"
                    "如果 NoneBot 和 NapCat 分容器部署，请把 Clonoth data/attachments 挂载到相同路径。"
                )
            try:
                faces, found = await self._wait_until(bot, lambda fs: _find_face(fs, md5=md5) is not None)
            except Exception:
                logger.warning("re-read favourites after add_custom_face failed", exc_info=True)
                return False, "NapCat 已接受收藏请求，但重新读取收藏列表失败，无法确认是否收藏成功。"
            target = _find_face(faces, md5=md5)
            if not found or target is None:
                return False, (
                    f"收藏表情失败：NapCat 返回成功，但收藏列表里找不到这张图片（md5 {md5[:8]}）。\n"
                    "可能是 QQ 收藏已满、图片格式不被接受，或 NapCat 无法读取该文件路径。"
                )
            head = f"已收藏表情：{alias}"
        ok, message = await self.set_desc(bot, target, alias)
        if not ok:
            where = "图片已在收藏中" if existing is not None else "图片已收藏"
            return False, f"{where}，但命名为「{alias}」失败：{message}"
        return True, head + "\n" + self.usage_note(alias)

    async def add_from_attachment(self, bot: Any, alias: str, attachment: Dict[str, Any]) -> str:
        path = attachment_abs_path(attachment, self.workspace)
        if path is None:
            return "找不到要收藏的图片文件，请重新发送图片后再试。"
        return (await self.add_image_file(bot, alias, path))[1]

    async def add_upload(self, bot: Any, alias: str, filename: str, data: bytes) -> tuple[bool, str]:
        """Favourite an uploaded image (web panel). The file is stored under
        data/attachments, which NapCat sees at the same path."""
        if not data:
            return False, "上传的文件为空。"
        if len(data) > config.IMAGE_MAX_BYTES:
            return False, f"图片超过 {config.IMAGE_MAX_BYTES // (1024 * 1024)} MB 上限。"
        mime = sniff_image_mime(data)
        if not mime:
            return False, "只能上传 PNG / JPEG / GIF / WebP / BMP 图片。"
        folder = Path(self.workspace or config.CLONOTH_WORKSPACE) / "data" / "attachments" / "qq_faces_upload"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{os.urandom(16).hex()}{image_ext_from_url_or_mime(filename or '', mime)}"
        path.write_bytes(data)
        return await self.add_image_file(bot, alias, path)

    async def face_by_res_id(self, bot: Any, res_id: str) -> Optional[Dict[str, Any]]:
        return _find_face(await self._live_faces(bot), res_id=str(res_id or ""))

    async def overview(self, bot: Any) -> Dict[str, Any]:
        """Every live favourite with its prompt visibility (web panel listing)."""
        faces = await self._live_faces(bot)
        names = self.current_names()
        positions = {normalize_face_name(n): i for i, n in enumerate(names)}
        items = [custom_face_summary(face, pos, self.bqbs) for pos, face in enumerate(faces)]
        counts: Dict[str, int] = {}
        for item in items:
            key = normalize_face_name(item["base_name"])
            if key:
                counts[key] = counts.get(key, 0) + 1
        for item in items:
            key = normalize_face_name(item["base_name"])
            pos = positions.get(key, -1) if key else -1
            item["duplicates"] = counts.get(key, 0) if key else 0
            item["prompt_position"] = pos + 1 if pos >= 0 else 0
            item["in_prompt"] = 0 <= pos < self.prompt_limit
        return {"prompt_limit": self.prompt_limit, "names_total": len(names), "faces": items}


    async def _resolve_unique(self, bot: Any, target: str, verb: str, example: str) -> tuple[Any, Optional[str]]:
        """Resolve a face; ask for an index when a base name is ambiguous."""
        try:
            siblings = await find_custom_faces_by_base_name(bot, target, self.bqbs)
        except Exception:
            logger.warning("find custom faces (%s) failed", verb, exc_info=True)
            return None, f"{verb}表情失败：{_DETAIL_UNSUPPORTED}"
        if len(siblings) > 1:
            return None, (
                f"检测到 {len(siblings)} 个同名表情“{target}”，为避免{'误删' if verb == '删除' else '改错'}，"
                f"请指定要{verb}哪一个：\n" + "\n".join(_sibling_lines(siblings))
                + f"\n请改用序号，例如：{example.format(index=siblings[0]['index'])}\n也可用 md5 或 resId 精确定位。"
            )
        try:
            return await resolve_custom_face(bot, target, self.bqbs), None
        except Exception:
            logger.warning("resolve custom face (%s) failed", verb, exc_info=True)
            return None, f"{verb}表情失败：{_DETAIL_UNSUPPORTED}"

    # ---- commands ------------------------------------------------------
    async def handle_command(
        self,
        *,
        bot: Any,
        event: Any,
        text: str,
        conversation_key: str,
        current_attachments: List[Dict[str, Any]],
        recent: RecentImages,
        is_admin: bool,
    ) -> Optional[str]:
        """Reply text for a custom-face command, or None when it is not one."""
        text = (text or "").strip()
        if not text:
            return None

        if HELP_RE.match(text):
            return HELP_TEXT if is_admin else "表情包命令仅限 Clonoth 管理员使用。"

        if SYNC_RE.match(text):
            if not is_admin:
                return "同步表情列表仅限 Clonoth 管理员使用。"
            try:
                names = await self.sync(bot)
            except Exception:
                logger.warning("sync custom face names failed", exc_info=True)
                return "同步表情列表失败：当前 OneBot 实现可能不支持 fetch_custom_face_detail。"
            if not names:
                return f"已同步，但没有发现已命名收藏表情。未命名表情不会写入 AI 表情列表文件：{self.names_path}"
            duplicates = count_duplicate_face_names(self.metadata)
            dup_note = ""
            if duplicates:
                desc = "、".join(f"{n}(x{c})" for n, c in list(duplicates.items())[:20])
                dup_note = (f"\n⚠ 检测到 {len(duplicates)} 组同名表情：{desc}\n"
                            "同名表情已全部保留，AI 名称列表仅显示一个基础名；发送时会在同名表情中随机选择一个。")
            return (f"已同步 {len(self.metadata)} 个已命名收藏表情（AI 可见基础名 {len(names)} 个）。\n"
                    f"AI 名称文件：{self.names_path}\n内部元数据文件：{self.metadata_path}\n"
                    "AI 只看到名称文件里的基础名，md5/resId/emojiId 保存在元数据文件里。" + dup_note)

        match = LIST_RE.match(text)
        if match:
            if not is_admin:
                return "表情列表仅限 Clonoth 管理员使用。"
            limit = _limit(match, 30)
            names = self.current_names()
            if not names:
                return (f"AI 表情列表文件为空：{self.names_path}\n"
                        "可发送“同步表情列表”从 NapCat 写入已命名表情；未命名表情不会写入。")
            more = "" if len(names) <= limit else f"\n……还有 {len(names) - limit} 个，可发送“表情列表 {min(len(names), 100)}”查看更多。"
            lines = [f"{i}. {n}" for i, n in enumerate(names[:limit], start=1)]
            return "AI 可用收藏表情：\n" + "\n".join(lines) + more + "\n模型可用格式：[表情:名称]"

        match = DETAIL_LIST_RE.match(text)
        if match:
            if not is_admin:
                return "表情详情列表仅限 Clonoth 管理员使用。"
            limit = _limit(match, 50)
            try:
                names = await list_custom_face_aliases(bot, self.bqbs, count=max(limit, 48))
            except Exception:
                logger.warning("list custom face details failed", exc_info=True)
                return "获取收藏表情详情失败：当前 OneBot 实现可能不支持 fetch_custom_face_detail。"
            if not names:
                return "当前没有可识别的收藏表情。"
            more = "" if len(names) <= limit else f"\n……还有 {len(names) - limit} 个，可发送“表情详情列表 {min(len(names), 100)}”查看更多。"
            lines = [f"{i}. {n}" for i, n in enumerate(names[:limit], start=1)]
            return "收藏表情详情（含未命名项）：\n" + "\n".join(lines) + more + "\n未命名表情可用：命名表情 <序号> <新名字>"

        match = RENAME_RE.match(text)
        if match:
            if not is_admin:
                return "命名/重命名表情仅限 Clonoth 管理员使用。"
            target, desc = match.group(1).strip(), match.group(2).strip()
            face, error = await self._resolve_unique(bot, target, "命名", "命名表情 {index} " + desc)
            if error:
                return error
            if face is not None and not isinstance(face, dict):
                return (f"找到了收藏表情：{target}，但当前 OneBot/NapCat 只返回图片 URL，没有 emojiId/resId/md5，无法命名。\n"
                        "请确认 NapCat 版本支持 fetch_custom_face_detail；仅旧 fetch_custom_face 返回的 URL 不能用于 set_custom_face_desc。")
            if not isinstance(face, dict):
                return f"没有找到收藏表情：{target}\n可先发送“表情详情列表 50”查看序号，再用“命名表情 <序号> <新名字>”。"
            return (await self.set_desc(bot, face, desc))[1]

        match = DELETE_RE.match(text)
        if match:
            if not is_admin:
                return "删除表情仅限 Clonoth 管理员使用。"
            name = match.group(1).strip()
            face, error = await self._resolve_unique(bot, name, "删除", "删除表情 {index}")
            if error:
                return error
            if face is not None and not isinstance(face, dict):
                return (f"找到了收藏表情：{name}，但当前 OneBot/NapCat 只返回图片 URL，没有 resId，无法删除。\n"
                        "请确认 NapCat 版本支持 fetch_custom_face_detail；仅旧 fetch_custom_face 返回的 URL 不能用于 delete_custom_face。")
            if not isinstance(face, dict):
                return f"没有找到收藏表情：{name}"
            ok, message = await self.delete_face(bot, face)
            return f"已删除收藏表情：{name}" if ok else message

        match = ADD_RE.match(text)
        if match:
            if not is_admin:
                return "收藏表情仅限 Clonoth 管理员使用。"
            alias = match.group(1).strip()
            images, errors = await custom_face_command_images(
                bot=bot, event=event, conversation_key=conversation_key,
                current=current_attachments, recent=recent, workspace=self.workspace,
            )
            if not images:
                suffix = "\n" + "\n".join(dict.fromkeys(errors)) if errors else ""
                return "没有找到可收藏的图片。请在同一条消息里带图，或引用/紧接着回复一张图片：收藏表情 名称" + suffix
            return await self.add_from_attachment(bot, alias, images[0])

        return None
