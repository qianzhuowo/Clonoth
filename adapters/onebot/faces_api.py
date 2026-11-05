"""Loopback admin API for QQ custom faces, used by the qq_faces web panel.

Served by the local bridge server (forward_bridge.py, 127.0.0.1 only, same
X-Forward-Token). Every handler delegates to the shared ``state.FACES``
(CustomFaces), so QQ commands and the panel run the same verified operations.

Routes (JSON in/out, ``ok`` + ``message`` on writes):
  GET  /qq_faces          live favourites with prompt visibility
  POST /qq_faces/sync     {}
  POST /qq_faces/rename   {res_id, name}
  POST /qq_faces/delete   {res_id}
  POST /qq_faces/add      {name, filename, data}   data = base64 image bytes
"""
from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, Awaitable, Callable

from . import config, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

# Base64 inflates uploads by 4/3; leave room for the JSON envelope.
MAX_REQUEST_BYTES = config.IMAGE_MAX_BYTES * 4 // 3 + 64 * 1024


def _bot() -> Any:
    from nonebot import get_bot

    return get_bot()


def register(app: Any, check_token: Callable[[Any], bool]) -> None:
    from aiohttp import web

    def guarded(handler: Callable[[Any], Awaitable[dict]]) -> Callable[[Any], Awaitable[Any]]:
        async def wrapped(request: Any) -> Any:
            if not check_token(request):
                return web.json_response({"ok": False, "message": "invalid token"}, status=403)
            payload: dict = {}
            if request.method == "POST" and request.can_read_body:
                try:
                    payload = await request.json()
                except Exception:
                    return web.json_response({"ok": False, "message": "invalid JSON"}, status=400)
                if not isinstance(payload, dict):
                    return web.json_response({"ok": False, "message": "invalid payload"}, status=400)
            try:
                bot = _bot()
            except Exception:
                return web.json_response({"ok": False, "message": "QQ Bot 尚未连接 NapCat。"}, status=503)
            try:
                return web.json_response(await handler(bot, payload))
            except Exception as exc:
                logger.warning("qq_faces admin API failed", exc_info=True)
                return web.json_response({"ok": False, "message": f"读取 NapCat 收藏失败：{exc}"}, status=502)

        return wrapped

    async def list_faces(bot: Any, _payload: dict) -> dict:
        return {"ok": True, **await state.FACES.overview(bot)}

    async def sync(bot: Any, _payload: dict) -> dict:
        names = await state.FACES.sync(bot)
        return {"ok": True, "message": f"已同步，AI 可见基础名 {len(names)} 个。"}

    async def located(bot: Any, payload: dict) -> tuple[Any, dict | None]:
        res_id = str(payload.get("res_id") or "").strip()
        face = await state.FACES.face_by_res_id(bot, res_id) if res_id else None
        if face is None:
            return None, {"ok": False, "message": "找不到该表情，可能已被删除，请刷新列表。"}
        return face, None

    async def rename(bot: Any, payload: dict) -> dict:
        face, error = await located(bot, payload)
        if error:
            return error
        ok, message = await state.FACES.set_desc(bot, face, str(payload.get("name") or ""))
        return {"ok": ok, "message": message}

    async def delete(bot: Any, payload: dict) -> dict:
        face, error = await located(bot, payload)
        if error:
            return error
        ok, message = await state.FACES.delete_face(bot, face)
        return {"ok": ok, "message": message}

    async def add(bot: Any, payload: dict) -> dict:
        try:
            data = base64.b64decode(str(payload.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            return {"ok": False, "message": "图片数据不是有效的 base64。"}
        ok, message = await state.FACES.add_upload(
            bot, str(payload.get("name") or ""), str(payload.get("filename") or ""), data,
        )
        return {"ok": ok, "message": message}

    app.router.add_get("/qq_faces", guarded(list_faces))
    app.router.add_post("/qq_faces/sync", guarded(sync))
    app.router.add_post("/qq_faces/rename", guarded(rename))
    app.router.add_post("/qq_faces/delete", guarded(delete))
    app.router.add_post("/qq_faces/add", guarded(add))
