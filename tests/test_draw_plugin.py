"""绘图插件包 plugins/draw/ 的行为测试。

覆盖：插件经真实加载器注册；finish_requires_tool（含异步占位修复）；生图渠道
门控（tool_visibility 面 + build_node_tool_specs 接线）；角色库注入；渠道可用性
注入；NovelAI 提示词模板（私有优先、模板回退）。
所有用例使用临时工作区与受控环境变量，不读取真实 .env。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.context import EngineContext
from engine.faces.prompt_sections import PromptSectionContext, PromptSectionRegistry
from engine.faces.tool_visibility import ToolVisibilityRegistry
from engine.hooks import HookContext, HookRegistry
from engine.hooks.loader import load_single_plugin

from plugins.draw import channels as draw_channels
from plugins.draw.character_inject import DrawCharacterInjector
from plugins.draw.finish_requires_tool import (
    REJECTION_CODE,
    FinishRequiresToolGate,
    FinishSuccessRecorder,
)
from plugins.draw.gating import image_gen_visibility
from plugins.draw.image_gen_inject import PLACEHOLDER, ImageGenAvailabilityInjector
from plugins.draw.novelai_prompts import render_novelai_prompts

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANNEL_ENV = ("CLONOTH_IMAGE_GPT_MODEL", "CLONOTH_IMAGE_GEMINI_MODEL")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_channel_env(monkeypatch):
    for key in CHANNEL_ENV:
        monkeypatch.delenv(key, raising=False)


def _node(node_id: str, extra: dict | None = None):
    return SimpleNamespace(id=node_id, extra=dict(extra or {}))


# ---------------------------------------------------------------- loading


def test_plugin_package_loads_through_real_loader():
    hooks = HookRegistry()
    ctx = EngineContext(hooks=hooks)
    visibility = ToolVisibilityRegistry()
    sections = PromptSectionRegistry()
    ctx.contributions.mount("tool_visibility", visibility)
    ctx.contributions.mount("prompt_sections", sections)

    meta = load_single_plugin(hooks, REPO_ROOT / "plugins", "draw", context=ctx, process="engine")

    assert meta.get("name") == "draw"
    assert "draw.image_gen_gating" in visibility.list_filters()
    # NovelAI 模板经真实注册路径以 static 作用域渲染给 draw.novelai_planner
    rendered = sections.render_messages(
        "static", PromptSectionContext(workspace_root=REPO_ROOT, node=_node("draw.novelai_planner")),
    )
    assert any("### TAG Guide" in str(m.get("content")) for m in rendered)
    assert sections.render_messages(
        "static", PromptSectionContext(workspace_root=REPO_ROOT, node=_node("qq.orchestrator")),
    ) == []


# ---------------------------------------------------- finish_requires_tool


class _FakeRctx:
    task_id = "t-1"

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit_event(self, name, payload):
        self.events.append((name, payload))


def _loop_state():
    return SimpleNamespace(plugin_data={}, rctx=_FakeRctx())


def _finish_ctx(node, ls, *, name="finish", messages=None):
    call = SimpleNamespace(id="call-1", name=name, arguments={"text": "done"})
    return HookContext(
        messages=list(messages or []), tools=[], node=node, provider=None, rctx=ls.rctx,
        step=3, tool_call=call, extra={"loop_state": ls, "terminal_call": call},
    )


def _after_ctx(node, ls, tool_name, result):
    return HookContext(
        messages=[], tools=[], node=node, provider=None, rctx=ls.rctx, step=2,
        extra={"loop_state": ls, "tool_name": tool_name, "tool_result": result},
    )


@pytest.fixture
def emitted(monkeypatch):
    import engine.inference.pseudo_handlers as ph

    calls: list[dict] = []

    def _fake_emit(ls, call, content, **kw):
        calls.append({"content": content, **kw})

    monkeypatch.setattr(ph, "_emit_pseudo_tool_result", _fake_emit)
    return calls


DRAW_NODE = _node("draw.image_gen", {"finish_requires_tool": {"any": ["gpt_image_2", "gemini_image"]}})


def test_finish_rejected_without_required_tool_success(emitted):
    ls = _loop_state()
    ctx = _finish_ctx(DRAW_NODE, ls)
    result = _run(FinishRequiresToolGate().handle(ctx))

    assert result is not None and result.channels.get("intercepted") is True
    assert ctx._chain_stopped is True
    assert len(emitted) == 1
    assert emitted[0]["meta_patch"]["tool_rejection_code"] == REJECTION_CODE
    assert "gemini_image, gpt_image_2" in emitted[0]["content"]
    assert ls.rctx.events and ls.rctx.events[0][1]["rejected"] is True


def test_finish_allowed_after_required_tool_succeeded(emitted):
    ls = _loop_state()
    _run(FinishSuccessRecorder().handle(_after_ctx(DRAW_NODE, ls, "gpt_image_2", {"ok": True})))
    assert _run(FinishRequiresToolGate().handle(_finish_ctx(DRAW_NODE, ls))) is None
    assert emitted == []


def test_failed_tool_result_does_not_satisfy_requirement(emitted):
    ls = _loop_state()
    _run(FinishSuccessRecorder().handle(_after_ctx(DRAW_NODE, ls, "gpt_image_2", {"ok": False, "error": "x"})))
    assert _run(FinishRequiresToolGate().handle(_finish_ctx(DRAW_NODE, ls))) is not None


def test_async_started_placeholder_in_history_allows_finish(emitted):
    """修复“Rejected finish 死循环”：异步结果回传 fork 出的新 task loop_state 为空。"""
    ls = _loop_state()
    history = [{"role": "tool", "content": '\u23f3 Async tool "gemini_image" started (id: a1). Result later.'}]
    assert _run(FinishRequiresToolGate().handle(_finish_ctx(DRAW_NODE, ls, messages=history))) is None
    assert emitted == []


def test_ask_is_not_gated(emitted):
    ls = _loop_state()
    assert _run(FinishRequiresToolGate().handle(_finish_ctx(DRAW_NODE, ls, name="ask"))) is None


def test_node_without_declaration_is_not_gated(emitted):
    ls = _loop_state()
    assert _run(FinishRequiresToolGate().handle(_finish_ctx(_node("qq.orchestrator"), ls))) is None


# ------------------------------------------------------------------ gating


def _vis_ctx(root: Path):
    return SimpleNamespace(node=_node("qq.orchestrator"), workspace_root=root)


def test_gating_hides_everything_when_no_channel_configured(tmp_path):
    hidden = image_gen_visibility(_vis_ctx(tmp_path))
    assert hidden == {"tools": {"gpt_image_2", "gemini_image"}, "delegate_targets": {"draw.image_gen"}}


def test_gating_reads_dotenv_and_keeps_draw_node(tmp_path):
    (tmp_path / ".env").write_text("CLONOTH_IMAGE_GEMINI_MODEL='gemini-test'\n", encoding="utf-8")
    hidden = image_gen_visibility(_vis_ctx(tmp_path))
    assert hidden == {"tools": {"gpt_image_2"}, "delegate_targets": set()}


def test_gating_hides_nothing_when_both_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("CLONOTH_IMAGE_GPT_MODEL", "gpt-test")
    monkeypatch.setenv("CLONOTH_IMAGE_GEMINI_MODEL", "gemini-test")
    assert image_gen_visibility(_vis_ctx(tmp_path)) is None


def test_build_node_tool_specs_applies_visibility(tmp_path, monkeypatch):
    import engine.inference.pseudo_tools as pt

    registry = ToolVisibilityRegistry()
    registry.register_filter("draw.image_gen_gating", image_gen_visibility)
    monkeypatch.setattr(pt, "tool_visibility_registry", registry)

    specs = [
        {"name": "gpt_image_2", "description": "", "input_schema": {"type": "object", "properties": {}}},
        {"name": "web_search", "description": "", "input_schema": {"type": "object", "properties": {}}},
    ]
    fake_registry = SimpleNamespace(list_specs=lambda: specs)
    monkeypatch.setattr(pt, "_filter_tool_specs", lambda node, all_specs: list(all_specs))

    node = SimpleNamespace(id="qq.orchestrator", name="QQ", delegate_targets=["draw.image_gen", "draw.novelai_planner"],
                           extra={}, tool_access=None)
    allowed, openai_tools = pt.build_node_tool_specs(
        registry=fake_registry, node=node, workspace_root=tmp_path,
    )

    assert "gpt_image_2" not in allowed and "web_search" in allowed
    names = [t["function"]["name"] for t in openai_tools]
    assert "gpt_image_2" not in names
    dispatch = next((t for t in openai_tools if t["function"]["name"] == "dispatch_to"), None)
    if dispatch is not None:
        assert "draw.image_gen" not in str(dispatch)
        assert "draw.novelai_planner" in str(dispatch)


# ------------------------------------------------------------- injections


def _write_drawtools(root: Path, rel: str, text: str) -> None:
    path = root / "tools" / "drawtools" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _prompt_ctx(root: Path, node_id: str, *, instruction="", messages=None):
    return HookContext(
        messages=list(messages or [{"role": "system", "content": "SYS"}, {"role": "user", "content": instruction}]),
        tools=[], node=_node(node_id), provider=None, rctx=SimpleNamespace(workspace_root=root),
        extra={"instruction_text": instruction, "history": []},
    )


CHAR_YAML = "characters:\n  - name: 测试角色\n    danbooru: test_character\n    aliases: [小测]\n    appearance: blue hair\n"


def test_character_inject_on_hit_for_planner(tmp_path):
    _write_drawtools(tmp_path, "character_tags.example.yaml", CHAR_YAML)
    ctx = _prompt_ctx(tmp_path, "draw.novelai_planner", instruction="画一张小测")
    result = _run(DrawCharacterInjector().handle(ctx))
    assert result is not None and result.modified
    assert ctx.messages[1]["role"] == "system" and "test_character" in ctx.messages[1]["content"]
    assert ctx.messages[2]["role"] == "user"


def test_character_inject_private_file_wins(tmp_path):
    _write_drawtools(tmp_path, "character_tags.example.yaml", CHAR_YAML)
    _write_drawtools(tmp_path, "character_tags.yaml", CHAR_YAML.replace("test_character", "private_tag"))
    ctx = _prompt_ctx(tmp_path, "draw.novelai_planner", instruction="画一张小测")
    _run(DrawCharacterInjector().handle(ctx))
    assert "private_tag" in ctx.messages[1]["content"]


def test_character_inject_skips_other_nodes_and_misses(tmp_path):
    _write_drawtools(tmp_path, "character_tags.example.yaml", CHAR_YAML)
    other = _prompt_ctx(tmp_path, "draw.image_gen", instruction="画一张小测")
    assert _run(DrawCharacterInjector().handle(other)) is None
    miss = _prompt_ctx(tmp_path, "draw.novelai_planner", instruction="画一只猫")
    assert _run(DrawCharacterInjector().handle(miss)) is None
    assert len(miss.messages) == 2


def test_image_gen_inject_replaces_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("CLONOTH_IMAGE_GPT_MODEL", "gpt-test")
    ctx = _prompt_ctx(tmp_path, "draw.image_gen", messages=[{"role": "system", "content": f"A {PLACEHOLDER} B"}])
    result = _run(ImageGenAvailabilityInjector().handle(ctx))
    content = ctx.messages[0]["content"]
    assert result is not None and PLACEHOLDER not in content
    assert "gpt_image_2：【可用】" in content and "gemini_image：【未配置" in content


def test_image_gen_inject_ignores_other_nodes(tmp_path):
    ctx = _prompt_ctx(tmp_path, "qq.orchestrator", messages=[{"role": "system", "content": PLACEHOLDER}])
    assert _run(ImageGenAvailabilityInjector().handle(ctx)) is None
    assert ctx.messages[0]["content"] == PLACEHOLDER


def test_draw_image_gen_node_carries_placeholder():
    text = (REPO_ROOT / "config" / "nodes" / "draw.image_gen.yaml").read_text(encoding="utf-8")
    assert PLACEHOLDER in text


def _section_ctx(root: Path, node_id: str):
    return SimpleNamespace(node=_node(node_id), workspace_root=root)


def test_novelai_prompts_order_and_template_fallback(tmp_path):
    for name in ("top-system", "tag-guide", "output-format"):
        _write_drawtools(tmp_path, f"prompts/novelai/{name}.example.md", f"EXAMPLE-{name}")
    _write_drawtools(tmp_path, "prompts/novelai/tag-guide.md", "PRIVATE-tag-guide")

    msg = render_novelai_prompts(_section_ctx(tmp_path, "draw.novelai_planner"))
    content = msg["content"]
    assert msg["role"] == "system" and content.startswith("## 绘图提示词模板")
    assert "PRIVATE-tag-guide" in content and "EXAMPLE-tag-guide" not in content
    assert content.index("### Top System") < content.index("### TAG Guide") < content.index("### Output Format")
    assert "EXAMPLE-top-system" in content and "EXAMPLE-output-format" in content


def test_novelai_prompts_only_for_planner(tmp_path):
    _write_drawtools(tmp_path, "prompts/novelai/top-system.example.md", "X")
    assert render_novelai_prompts(_section_ctx(tmp_path, "draw.image_gen")) is None


def test_repo_ships_novelai_example_templates():
    """仓库自带三份模板，私有文件缺失时 draw.novelai_planner 仍有完整提示词。"""
    msg = render_novelai_prompts(_section_ctx(REPO_ROOT, "draw.novelai_planner"))
    assert msg is not None
    for title in ("### Top System", "### TAG Guide", "### Output Format"):
        assert title in msg["content"]


def test_channel_check_is_single_source():
    assert draw_channels.IMAGE_GEN_TOOLS == {"gpt_image_2", "gemini_image"}
