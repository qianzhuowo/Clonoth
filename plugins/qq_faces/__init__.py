"""QQ 收藏表情管理面板：预览、重命名、删除、上传添加、同步。

设置区面板（iframe 加载 web/index.html）。本插件只做转发：QQ Bot 进程
（adapters/onebot）持有 NapCat 连接，表情操作全部在那里执行（faces_api.py，
复用 CustomFaces，与 QQ 内的表情命令同一套校验逻辑）。

转发目标是 QQ Bot 进程的本机 bridge：ONEBOT_FORWARD_BRIDGE_HOST/PORT，
令牌 ONEBOT_FORWARD_BRIDGE_TOKEN（与 qq_forward 工具共用）。
supervisor 与 QQ Bot 读取同一份 .env。
"""

import os
from pathlib import Path

PLUGIN_META = {
    "name": "qq_faces",
    "version": "1.0.0",
    "description": "QQ 收藏表情管理：缩略图预览、重命名、删除、拖拽上传、同名与提示上限标记",
    "author": "clonoth",
    "processes": ["supervisor"],
    "web": {
        "panels": [
            {
                "id": "manager",
                "slot": "settings",
                "order": 60,
                "title": "QQ 表情",
                "entry": "/v1/plugins/qq_faces/web/",
            }
        ],
    },
}

_TIMEOUT_SECONDS = 90.0


def _bridge() -> tuple[str, dict]:
    host = (os.environ.get("ONEBOT_FORWARD_BRIDGE_HOST") or "127.0.0.1").strip()
    port = (os.environ.get("ONEBOT_FORWARD_BRIDGE_PORT") or "8769").strip()
    token = (os.environ.get("ONEBOT_FORWARD_BRIDGE_TOKEN") or "").strip()
    return f"http://{host}:{port}/qq_faces", ({"X-Forward-Token": token} if token else {})


def register(ctx) -> None:
    routes = getattr(ctx.contributions, "get", lambda _name: None)("routes")
    if routes is None:
        return

    import httpx
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse

    from engine.faces.routes import static_router

    api = APIRouter()

    async def _forward(method: str, path: str, body: dict | None = None) -> JSONResponse:
        base, headers = _bridge()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.request(method, base + path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"ok": False, "message": f"连不上 QQ Bot 进程（{base}）：{type(exc).__name__}。请确认 QQ Bot（OneBot 适配器）进程正在运行。"},
                status_code=502,
            )
        try:
            data = resp.json()
        except ValueError:
            data = {"ok": False, "message": f"QQ Bot 返回了非 JSON 响应（HTTP {resp.status_code}）。"}
        return JSONResponse(data, status_code=resp.status_code)

    @api.get("/list")
    async def _list() -> JSONResponse:
        return await _forward("GET", "")

    @api.post("/sync")
    async def _sync() -> JSONResponse:
        return await _forward("POST", "/sync", {})

    async def _body(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @api.post("/rename")
    async def _rename(request: Request) -> JSONResponse:
        return await _forward("POST", "/rename", await _body(request))

    @api.post("/delete")
    async def _delete(request: Request) -> JSONResponse:
        return await _forward("POST", "/delete", await _body(request))

    @api.post("/add")
    async def _add(request: Request) -> JSONResponse:
        return await _forward("POST", "/add", await _body(request))

    # register 必须在路由定义之后（已 attach 时只拷贝当时存在的路由）。
    routes.register(api, description="qq_faces 表情管理 API（转发到 QQ Bot 本机 bridge，需鉴权）")

    client = APIRouter()
    client.include_router(static_router(Path(__file__).parent / "web"), prefix="/web")
    routes.register(client, description="qq_faces 面板静态资源（需鉴权）")
