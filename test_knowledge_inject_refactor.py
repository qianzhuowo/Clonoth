from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace


def test_knowledge_match_keyword_and_scan_text_semantics() -> None:
    """Protect the shared matcher against behavior drift during the refactor."""
    # Why: skills and memory used identical keyword semantics before this refactor.
    # How: exercise regex flags, invalid-regex fallback, substring matching, and
    # round-based history scanning through the new shared module. Purpose: keep
    # both injection paths byte-compatible with the duplicated legacy functions.
    from engine.knowledge_match import build_scan_text, compile_keyword, match_keywords

    assert match_keywords([compile_keyword("/hello/i")], "HeLLo world") is True
    assert match_keywords([compile_keyword("Broken[")], "a broken[ keyword") is True
    assert match_keywords([compile_keyword("Needle")], "haystack needle") is True
    assert match_keywords([compile_keyword("")], "needle") is False

    history = [
        {"role": "user", "content": "old user"},
        {"role": "assistant", "content": "old assistant"},
        {"role": "system", "content": "ignored system"},
        {"role": "user", "content": "recent user"},
        {"role": "assistant", "content": "recent assistant"},
    ]
    scan_text = build_scan_text("current instruction", history, 1)
    assert scan_text == "current instruction\nrecent user\nrecent assistant"


def test_knowledge_inject_sets_existing_extra_keys_and_rebuilds_prompt(tmp_path: Path) -> None:
    """Verify the unified injector preserves the old prompt injection contract."""
    # Why: run_ai_node and downstream code still read the existing skill_* and
    # memory_* ctx.extra keys. How: create a tiny workspace, run KnowledgeInjector,
    # and compare its rebuilt messages with the shared assembly helper. Purpose:
    # ensure the unified plugin changes ownership, not prompt layout or key names.
    skills_dir = tmp_path / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo\n"
        "description: Demo skill\n"
        "enabled: true\n"
        "strategy: normal\n"
        "keywords: [demo]\n"
        "scan_depth: 0\n"
        "---\n"
        "Demo skill body.\n",
        encoding="utf-8",
    )

    memory_dir = tmp_path / "data" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "book.yaml").write_text(
        "book: book\n"
        "entries:\n"
        "  - id: always\n"
        "    content: Constant memory body.\n"
        "    constant: true\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    from engine.builtin.knowledge_inject import KnowledgeInjector
    from engine.inference.message_assembly import assemble_messages_with_injections

    node = SimpleNamespace(
        id="node-a",
        skill_access=SimpleNamespace(mode="all", allow=None),
        memory_access=SimpleNamespace(mode="all", allow=None),
    )
    system_prompt = [{"role": "system", "content": "System prompt."}]
    history = [{"role": "user", "content": "previous turn"}]
    ctx = SimpleNamespace(
        node=node,
        rctx=SimpleNamespace(workspace_root=tmp_path),
        messages=[{"role": "system", "content": "placeholder"}],
        extra={
            "runtime_cfg": {"skills": {"max_budget_chars": 0}, "memory": {"max_budget_chars": 0}},
            "instruction_text": "please use demo",
            "history": history,
            "attachments": None,
            "system_prompt": system_prompt,
            "apply_injection": True,
        },
    )

    result = asyncio.run(KnowledgeInjector().handle(ctx))

    assert result is not None
    assert result.modified is True
    assert ctx.extra["skill_static_messages"] == []
    assert ctx.extra["skill_dynamic_messages"]
    assert ctx.extra["memory_static_messages"]
    assert ctx.extra["memory_dynamic_messages"] == []

    expected_messages, expected_block_mode = assemble_messages_with_injections(
        workspace_root=tmp_path,
        system_prompt=system_prompt,
        history=history,
        instruction="please use demo",
        attachments=None,
        skill_static=ctx.extra["skill_static_messages"],
        skill_dynamic=ctx.extra["skill_dynamic_messages"],
        memory_static=ctx.extra["memory_static_messages"],
        memory_dynamic=ctx.extra["memory_dynamic_messages"],
    )
    assert ctx.messages == expected_messages
    assert ctx.extra["is_block_mode"] is expected_block_mode
