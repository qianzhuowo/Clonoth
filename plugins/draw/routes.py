"""绘图管理 API（supervisor 进程）：/v1/plugins/draw/…，全部需管理 token。

只做 HTTP 壳：参数取出 → plugins/draw/admin.py → JSON。workspace_root 来自
app.state（engine/faces/routes.PluginRoutesFace.attach 注入）。

注意：不能用 ``from __future__ import annotations``——FastAPI 需要在运行时解析 ``Request`` 注解，
字符串注解会让它把 request 当成查询参数。
"""

from pathlib import Path
from typing import Any

from . import admin


def build_api_router() -> Any:
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse

    api = APIRouter()

    def _root(request: Request) -> Path:
        root = getattr(request.app.state, "workspace_root", None)
        return Path(root) if root else Path.cwd()

    async def _body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _fail(error: admin.AdminError) -> JSONResponse:
        payload: dict[str, Any] = {"ok": False, "message": error.message}
        if error.details is not None:
            payload["details"] = error.details
        return JSONResponse(payload, status_code=error.status)

    def _ok(data: Any) -> JSONResponse:
        return JSONResponse({"ok": True, "data": data})

    # ---- settings ----
    @api.get("/settings")
    async def _get_settings(request: Request) -> JSONResponse:
        return _ok(admin.get_settings_view(_root(request)))

    @api.post("/settings")
    async def _post_settings(request: Request) -> JSONResponse:
        try:
            return _ok(admin.update_settings(_root(request), await _body(request)))
        except admin.AdminError as error:
            return _fail(error)

    # ---- scene agent (planner model) ----
    @api.get("/agent")
    async def _get_agent(request: Request) -> JSONResponse:
        return _ok(admin.get_agent_view(_root(request)))

    @api.post("/agent")
    async def _post_agent(request: Request) -> JSONResponse:
        try:
            return _ok(admin.save_agent(_root(request), await _body(request)))
        except admin.AdminError as error:
            return _fail(error)

    @api.post("/agent/models")
    async def _agent_models(request: Request) -> JSONResponse:
        try:
            return _ok(await admin.pull_agent_models(_root(request), await _body(request)))
        except admin.AdminError as error:
            return _fail(error)

    @api.post("/agent/test")
    async def _agent_test(request: Request) -> JSONResponse:
        try:
            return _ok(await admin.check_agent_connection(_root(request), await _body(request)))
        except admin.AdminError as error:
            return _fail(error)

    # ---- presets ----
    @api.get("/presets")
    async def _get_presets(request: Request) -> JSONResponse:
        return _ok(admin.list_presets(_root(request)))

    @api.post("/presets")
    async def _post_presets(request: Request) -> JSONResponse:
        body = await _body(request)
        try:
            return _ok(admin.save_presets(_root(request), body.get("presets") or [], body.get("selected_preset_id")))
        except admin.AdminError as error:
            return _fail(error)

    @api.post("/presets/export")
    async def _export_preset(request: Request) -> JSONResponse:
        try:
            return _ok(admin.export_preset(await _body(request)))
        except admin.AdminError as error:
            return _fail(error)

    @api.post("/presets/import")
    async def _import_preset(request: Request) -> JSONResponse:
        try:
            return _ok(admin.import_preset(await _body(request)))
        except admin.AdminError as error:
            return _fail(error)

    # ---- characters ----
    @api.get("/characters")
    async def _get_characters(request: Request) -> JSONResponse:
        try:
            return _ok(admin.get_character_library(_root(request)))
        except admin.AdminError as error:
            return _fail(error)

    @api.post("/characters")
    async def _post_characters(request: Request) -> JSONResponse:
        try:
            return _ok(admin.save_character_library(_root(request), await _body(request)))
        except admin.AdminError as error:
            return _fail(error)

    @api.post("/characters/export")
    async def _export_characters(request: Request) -> JSONResponse:
        try:
            return _ok(admin.export_characters(_root(request), await _body(request)))
        except admin.AdminError as error:
            return _fail(error)

    @api.post("/characters/import")
    async def _import_characters(request: Request) -> JSONResponse:
        try:
            return _ok(admin.import_characters(_root(request), await _body(request)))
        except admin.AdminError as error:
            return _fail(error)

    # ---- prompts ----
    @api.get("/prompts")
    async def _get_prompts(request: Request) -> JSONResponse:
        return _ok(admin.get_prompts(_root(request)))

    @api.post("/prompts/{key}")
    async def _post_prompt(key: str, request: Request) -> JSONResponse:
        body = await _body(request)
        try:
            return _ok(admin.save_prompt(_root(request), key, str(body.get("content") or "")))
        except admin.AdminError as error:
            return _fail(error)

    @api.post("/prompts/{key}/reset")
    async def _reset_prompt(key: str, request: Request) -> JSONResponse:
        try:
            return _ok(admin.reset_prompt(_root(request), key))
        except admin.AdminError as error:
            return _fail(error)

    # ---- layout / fields / preview ----
    @api.get("/layout")
    async def _get_layout(request: Request) -> JSONResponse:
        return _ok(admin.get_layout(_root(request)))

    @api.post("/layout")
    async def _post_layout(request: Request) -> JSONResponse:
        body = await _body(request)
        try:
            return _ok(admin.save_layout(_root(request), body.get("layout")))
        except admin.AdminError as error:
            return _fail(error)

    @api.get("/fields")
    async def _get_fields(request: Request) -> JSONResponse:
        return _ok(admin.get_field_descriptions(_root(request)))

    @api.post("/preview")
    async def _preview(request: Request) -> JSONResponse:
        body = await _body(request)
        try:
            chain = admin.preview_chain(_root(request), layout=body.get("layout"), texts_override=body.get("texts"))
        except admin.AdminError as error:
            return _fail(error)
        return _ok({"chain": chain})

    # ---- images ----
    @api.get("/images")
    async def _get_images(request: Request) -> JSONResponse:
        return _ok(admin.list_images(_root(request)))

    @api.get("/images/file/{name}")
    async def _image_file(name: str, request: Request) -> Any:
        from fastapi.responses import FileResponse

        folder = (_root(request) / admin.ATTACHMENTS_REL).resolve()
        target = (folder / Path(name.replace("\\", "/")).name).resolve()
        if target.parent != folder or not target.is_file() or target.suffix.lower() not in admin.IMAGE_SUFFIXES:
            return JSONResponse({"ok": False, "message": "not found"}, status_code=404)
        return FileResponse(str(target))

    @api.post("/images/delete")
    async def _delete_images(request: Request) -> JSONResponse:
        body = await _body(request)
        return _ok(admin.delete_images(_root(request), [str(n) for n in (body.get("names") or [])]))

    @api.post("/images/cleanup")
    async def _cleanup_images(request: Request) -> JSONResponse:
        return _ok(admin.cleanup_images(_root(request)))

    # ---- quick test ----
    @api.post("/test")
    async def _quick_test(request: Request) -> JSONResponse:
        try:
            return _ok(await admin.quick_test(_root(request), await _body(request)))
        except admin.AdminError as error:
            return _fail(error)

    return api
