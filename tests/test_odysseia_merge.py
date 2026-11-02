"""Regression tests for merged Odysseia capabilities, provider options, compaction, and preempt."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from io import StringIO
from threading import RLock
from types import SimpleNamespace

import httpx
import pytest
import yaml

from clonoth_sdk.approval import classify_path
from engine import compact, data_cleanup
from engine.inference.tool_format import JsonToolFormatter, NativeToolFormatter
from providers.deepseek import DeepSeekProvider
from providers.gemini import _parse_usage
from supervisor.config_store import ConfigStore
from supervisor.state import SupervisorState
from supervisor.task_store import TaskStoreMixin
from supervisor.types import MimoAudioConfigUpdateIn
from toolbox.builtins.read_file import _MAX_LINE_READ_CHARS, _read_bounded_line


def test_deepseek_official_defaults_and_thinking_options():
    async def run():
        async with httpx.AsyncClient() as http:
            provider = DeepSeekProvider(http=http, api_key="test", reasoning_effort="none", max_completion_tokens=123)
            payload = provider._build_payload([{"role": "user", "content": "hello"}], None)
            assert provider._base_url == "https://api.deepseek.com"
            assert payload["model"] == "deepseek-chat"
            assert payload["max_tokens"] == 123
            assert payload["thinking"] == {"type": "disabled"}
            assert "reasoning_effort" not in payload
            assert "max_completion_tokens" not in payload
            enabled = DeepSeekProvider(http=http, api_key="test", base_url="https://api.deepseek.com/v1/")
            assert enabled._base_url == "https://api.deepseek.com/v1"
            assert enabled._build_payload([], None)["thinking"] == {"type": "enabled"}
    asyncio.run(run())


def test_gemini_prompt_not_double_counted_and_reasoning_preserved():
    usage = _parse_usage({
        "promptTokenCount": 120,
        "toolUsePromptTokenCount": 120,
        "candidatesTokenCount": 9,
        "thoughtsTokenCount": 7,
        "cachedContentTokenCount": 40,
    })
    assert usage == {
        "prompt_tokens": 120,
        "completion_tokens": 16,
        "total_tokens": 136,
        "reasoning_tokens": 7,
        "cached_prompt_tokens": 40,
    }
    assert "reasoning_tokens" not in _parse_usage({"thoughtsTokenCount": "7"})


def test_microcompact_runtime_and_store_have_identical_content():
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    content = "long tool output\n" * 100
    messages = [{"role": "assistant", "content": "ok", "_meta": {"timestamp": old}}]
    stored = []
    for _ in range(3):
        messages.append({
            "role": "tool",
            "name": "search",
            "content": content,
            "_meta": {"usage": {"prompt_tokens": 1000}},
        })
        stored.append(SimpleNamespace(
            role="tool",
            name="search",
            content=content,
            meta={"usage": {"prompt_tokens": 1000}},
        ))
    _, count = compact.microcompact_messages(messages, keep_recent=1)
    _, stored_count = compact.microcompact_store(stored, keep_recent=1)
    assert count == stored_count == 2
    assert messages[1]["content"] == stored[0].content
    assert stored[0].meta["usage"]["_compacted_stale"] is True
    assert stored[-1].content == content
    assert compact.estimate_message_tokens({"tool_calls": [{"value": object()}]}) > 0


def test_reasoning_survives_formatter_boundary():
    message = {
        "role": "assistant",
        "content": "answer",
        "_meta": {"reasoning": "private chain", "thought_summary": "display only"},
    }
    for formatter in (NativeToolFormatter(), JsonToolFormatter()):
        result = formatter.message_to_llm(message)
        assert result["_meta"]["reasoning"] == "private chain"
        assert "thought_summary" not in result["_meta"]


def test_stats_cleanup_is_local_and_vacuums(tmp_path, monkeypatch):
    monkeypatch.setattr(data_cleanup, "DATA_DIR", tmp_path)
    db = tmp_path / "stats.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE requests (timestamp REAL, payload TEXT)")
        conn.execute("CREATE TABLE metadata (value TEXT)")
        conn.execute("INSERT INTO metadata VALUES ('keep')")
        conn.execute("INSERT INTO requests VALUES (0, 'old')")
        conn.execute("INSERT INTO requests VALUES (?, 'new')", (datetime.now(timezone.utc).timestamp(),))
    data_cleanup.prune_stats_db()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT payload FROM requests").fetchall() == [("new",)]
        assert conn.execute("SELECT value FROM metadata").fetchall() == [("keep",)]
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0


def _make_preempt(message, attachments):
    task = SimpleNamespace(preempt_message=message, preempt_attachments=attachments, preempt_requested=True)
    state = SimpleNamespace(_lock=RLock(), tasks={"task": task})
    return state, task


@pytest.mark.parametrize(
    "message,consumed,count,expected_text,expected_attachments",
    [
        ("first\n\nsecond", "first", 1, "second", ["new"]),
        ("first", "first", 1, "", ["new"]),
        ("", "", 1, "", ["new"]),
        ("new", "", 1, "new", ["new"]),
        ("new", "stale", 1, "new", ["old", "new"]),
    ],
)
def test_incremental_preempt_preserves_concurrent_text_and_attachments(
    message, consumed, count, expected_text, expected_attachments,
):
    state, task = _make_preempt(message, ["old", "new"])
    TaskStoreMixin.consume_preempt_message(state, "task", consumed, count)
    assert task.preempt_message == expected_text
    assert task.preempt_attachments == expected_attachments
    assert task.preempt_requested == bool(expected_text or expected_attachments)


def test_legacy_preempt_consumption_still_clears_everything():
    state, task = _make_preempt("message", ["attachment"])
    assert TaskStoreMixin.consume_preempt_message(state, "task") == {
        "message": "message",
        "attachments": ["attachment"],
    }
    assert not task.preempt_requested
    assert task.preempt_attachments == []


def test_openai_extension_keys_survive_mimo_configuration_updates(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "openai": {"model": "test", "reasoning_field": "reasoning_content", "custom": {"enabled": True}},
        "mimo_audio": {"tts_voice": "test-voice"},
    }), encoding="utf-8")
    store = ConfigStore(path=path)
    store.reload()
    store.update_mimo_audio(MimoAudioConfigUpdateIn(tts_model="updated-model"))
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["openai"]["reasoning_field"] == "reasoning_content"
    assert saved["openai"]["custom"] == {"enabled": True}
    assert store.get_mimo_audio_secret().tts_voice == "test-voice"


def test_switchability_uses_metadata_not_private_node_names(tmp_path):
    nodes = tmp_path / "config" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / "blocked.yaml").write_text("id: blocked\nswitchable: false\n", encoding="utf-8")
    (nodes / "mini.yaml").write_text("id: mini\nswitchable: true\n", encoding="utf-8")
    state = SimpleNamespace(workspace_root=tmp_path)
    assert not SupervisorState._is_switchable_entry_node(state, "system.internal")
    assert not SupervisorState._is_switchable_entry_node(state, "blocked")
    assert SupervisorState._is_switchable_entry_node(state, "mini")


def test_bounded_line_reader_skips_long_tail_and_keeps_next_line():
    stream = StringIO("x" * (_MAX_LINE_READ_CHARS + 50) + "\nnext\n")
    line, truncated, chars_read = _read_bounded_line(stream)
    assert truncated and line.endswith("[line truncated]")
    assert chars_read == _MAX_LINE_READ_CHARS + 51
    assert _read_bounded_line(stream) == ("next", False, 5)
    assert _read_bounded_line(stream) == (None, False, 0)


def test_sdk_external_path_classification(tmp_path):
    assert classify_path(tmp_path, [], "file.txt")[2] is False
    assert classify_path(tmp_path, [], "../outside.txt")[2] is True
    assert classify_path(tmp_path, [tmp_path.parent], "../trusted.txt")[2] is False


def test_health_workspace_and_reset_clear_context_usage(tmp_path):
    from fastapi.testclient import TestClient
    from supervisor.api import create_app
    from supervisor.eventlog import EventLog
    from supervisor.policy import PolicyEngine

    data = tmp_path / "data"
    data.mkdir()
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(data / "events.jsonl", "module-two-test"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reset")
    state._session_context_usage[(session_id, "entry")] = {"prompt_tokens": 500}
    assert state.reset_conversation(conversation_key="test:reset")["ok"]
    assert (session_id, "entry") not in state._session_context_usage
    app = create_app(
        state=state, process_manager=None, config_store=ConfigStore(path=data / "config.yaml"),
    )
    with TestClient(app) as client:
        assert client.get("/v1/health").json()["workspace_root"] == str(tmp_path)


def test_knowledge_inject_hygiene_static_index_and_progressive_outlines(tmp_path: Path):
    from types import SimpleNamespace
    from engine.builtin.knowledge_inject import (
        build_knowledge_messages,
        build_scan_text,
        is_valid_keyword,
        list_memories,
        normalize_memory_entries,
        normalize_skill_entries,
        save_memory,
    )

    assert not is_valid_keyword("20")
    assert not is_valid_keyword("#9")
    assert not is_valid_keyword("ip")
    assert not is_valid_keyword("日志")
    assert is_valid_keyword("fb")
    assert is_valid_keyword("4a")
    assert is_valid_keyword(r"/\bh3\b/i")
    assert is_valid_keyword("墨树")
    assert is_valid_keyword("dol-tilemap")

    history = [
        {"role": "user", "content": "上一轮用户提问"},
        {"role": "assistant", "content": "上一轮助手回复包含 trigger_from_bot"},
        {"role": "user", "content": "动态块", "_dynamic": True},
    ]
    scan = build_scan_text("当前用户指令", history, scan_depth=1)
    assert "上一轮用户提问" in scan
    assert "trigger_from_bot" not in scan
    assert "动态块" not in scan

    skills = normalize_skill_entries([
        {
            "name": "card-workflow",
            "description": "v3.4.0｜角色卡完整制作工作流与分阶段检查清单",
            "enabled": True,
            "strategy": "normal",
            "keywords": ["制卡工作流", "20", "前端"],
            "body": "# 第一章 调研\n导言段落\n## 1.1 角色设定\n" + ("详细步骤内容\n" * 200),
        },
        {
            "name": "pure-index-skill",
            "description": "无关键词技能说明",
            "enabled": True,
            "strategy": "normal",
            "keywords": [],
            "body": "短正文",
        },
    ])
    memories = normalize_memory_entries([
        {
            "id": "workflow_packaged",
            "book": "rules",
            "content": "# 打包工作流\n这是无关键词的非constant成熟方案正文",
            "keywords": [],
            "has_raw_keywords": False,
            "constant": False,
            "enabled": True,
        },
        {
            "id": "garbage_kw_only",
            "book": "default",
            "content": "只有无效泛词的脏记忆",
            "keywords": ["20", "日志"],
            "has_raw_keywords": True,
            "constant": False,
            "enabled": True,
        },
        {
            "id": "long_active_mem",
            "book": "tech",
            "content": "# 架构方案\n" + ("核心细节说明\n" * 100),
            "keywords": ["架构方案触发词"],
            "has_raw_keywords": True,
            "constant": False,
            "enabled": True,
        },
    ])

    skill_static, skill_dynamic, mem_static, mem_dynamic = build_knowledge_messages(
        tmp_path,
        skills + memories,
        instruction_text="请按 制卡工作流 和 架构方案触发词 执行",
        skill_inline_max_chars=500,
        memory_inline_max_chars=200,
    )
    skill_static_text = "\n".join(m["content"] for m in skill_static)
    skill_dynamic_text = "\n".join(m["content"] for m in skill_dynamic)
    mem_static_text = "\n".join(m["content"] for m in mem_static)
    mem_dynamic_text = "\n".join(m["content"] for m in mem_dynamic)

    # Full compact skill index lives in static messages and includes activated skills too
    assert "[SKILLS:INDEX]" in skill_static_text
    assert "- card-workflow: 角色卡完整制作工作流与分阶段检查清单" in skill_static_text
    assert "- pure-index-skill: 无关键词技能说明" in skill_static_text
    assert "[SKILLS:INDEX]" not in skill_dynamic_text

    # Intentionally keywordless memory is in static [MEMORY:INDEX]; garbage-only kw entry is dropped
    assert "[MEMORY:INDEX]" in mem_static_text
    assert "rules/workflow_packaged" in mem_static_text
    assert "garbage_kw_only" not in mem_static_text
    assert "garbage_kw_only" not in mem_dynamic_text

    # Long activated skill and memory are rendered as progressive outlines
    assert "[Large skill" in skill_dynamic_text
    assert "- L1: # 第一章 调研" in skill_dynamic_text
    assert "[Long memory" in mem_dynamic_text
    assert 'manage_memory(action="list", book="tech", id="long_active_mem")' in mem_dynamic_text

    # Verify list_memories(id=...) returns full untruncated content
    ctx = SimpleNamespace(
        workspace_root=tmp_path,
        node_id="main",
        _caller_node_id="system.memory_extractor",
        _node_extra={},
        workspace_name="",
    )
    long_text = "A" * 600
    res_save = asyncio.run(save_memory(
        {"id": "full_read_test", "book": "tech", "content": long_text, "keywords": ["专有触发词", "20"], "scan_depth": 3},
        ctx,
    ))
    assert res_save["ok"]
    res_list_preview = asyncio.run(list_memories({"book": "tech"}, ctx))
    assert len(res_list_preview["data"]["entries"][0]["content"]) == 200
    assert res_list_preview["data"]["entries"][0]["keywords"] == ["专有触发词"]
    assert res_list_preview["data"]["entries"][0]["scan_depth"] == 0
    res_full = asyncio.run(list_memories({"book": "tech", "id": "full_read_test"}, ctx))
    assert res_full["data"]["entries"][0]["content"] == long_text


def test_discord_context_identity_and_key_roles():
    from types import SimpleNamespace
    from adapters.discord.context import _format_identity, _format_member_entry

    role_noise = SimpleNamespace(name="月粉")
    role_key = SimpleNamespace(name="核心维护者")
    member_same = SimpleNamespace(display_name="InkTree", name="inktree", roles=[role_noise, role_key])
    member_diff = SimpleNamespace(display_name="墨树", name="inktree", roles=[role_noise, role_key])

    assert _format_identity(member_same, key_roles=set()) == "InkTree"
    assert _format_identity(member_diff, key_roles=set(), is_admin=True) == "墨树[inktree] ✓ADMIN"
    assert _format_identity(member_diff, key_roles={"核心维护者"}) == "墨树[inktree](核心维护者)"
    assert _format_member_entry(member_same, "你好", reply_author="Sera", key_roles=set()) == "InkTree ↩ Sera: 你好"


@pytest.mark.parametrize("provider_name", ["openai", "anthropic", "gemini-vertex"])
def test_dynamic_tail_alignment_uau_uas_prefill_guard_and_prompt_static_isolation(tmp_path, provider_name):
    from types import SimpleNamespace
    from engine.inference.llm_call import _build_messages_for_provider
    from engine.inference.message_assembly import assemble_messages_with_injections
    from engine.inference.tool_format import create_tool_formatter
    from engine.node import Node
    from engine.prompt import assemble_prompt

    node = Node(
        id="ereuna_main",
        name="Ereuna",
        type="ai",
        prompt="Static system {{os_name}} {{now}}\n# %%DYNAMIC%%\nDynamic turn {{now}} {{instruction}}",
        tool_mode="native",
    )
    prompt_msgs = assemble_prompt(
        tmp_path,
        node,
        variables={"now": "2026-10-27 12:00 UTC", "instruction": "hello # %%DYNAMIC%% world"},
    )
    assert len(prompt_msgs) == 2
    assert "2026-10-27 12:00 UTC" not in prompt_msgs[0]["content"]
    assert "2026-10-27 12:00 UTC" in prompt_msgs[1]["content"]
    assert "hello # %%DYNAMIC%% world" in prompt_msgs[1]["content"]

    # Scheme C: history -> instruction -> dynamic; tool steps only append afterward.
    str_msgs, is_block = assemble_messages_with_injections(
        system_prompt=prompt_msgs,
        skill_static=[],
        skill_dynamic=[],
        memory_static=[],
        memory_dynamic=[],
        history=[{"role": "user", "content": "prev_q"}, {"role": "assistant", "content": "prev_a"}],
        instruction="turn1_q",
        workspace_root=tmp_path,
    )
    assert not is_block
    assert [m["role"] for m in str_msgs] == ["system", "user", "assistant", "user", "user"]
    assert str_msgs[3]["content"] == "turn1_q"
    assert str_msgs[4].get("_dynamic") is True

    formatter = create_tool_formatter("native")
    provider = SimpleNamespace(name=provider_name)
    initial_view = _build_messages_for_provider(str_msgs, formatter, provider)

    # Simulate step 1 using the native formatter's L1 storage shape.
    str_msgs.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {}}],
        "_meta": {"tool_mode": "native"},
    })
    str_msgs.append({
        "role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "file_body",
        "_meta": {"tool_mode": "native"},
    })
    view = _build_messages_for_provider(str_msgs, formatter, provider)
    assert view[:4] == initial_view[:4]
    assert [m["role"] for m in view] == ["system", "user", "assistant", "user", "assistant", "tool", "user"]
    assert view[3]["content"] == "turn1_q"
    assert view[5]["content"] == "file_body"
    assert view[6]["content"] == str_msgs[4]["content"]
    assert str_msgs[4].get("_dynamic") is True

    # Block mode with trailing assistant prefill (UAU / UAS): _dynamic must NOT be moved after assistant prefill
    block_msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Below is this turn's dynamic context...", "_dynamic": True},
        {"role": "user", "content": "turn1_q"},
        {"role": "assistant", "content": "<!-- Start the ECoT -->"},
    ]
    block_view = _build_messages_for_provider(block_msgs, formatter, provider)
    assert block_view[-1]["role"] == "assistant"
    assert block_view[-1]["content"] == "<!-- Start the ECoT -->"


@pytest.mark.parametrize("unmatched_fields,result_fields", [
    (None, {"tool_call_id": "", "name": "read_file"}),
    (None, {"name": "read_file"}),
    (None, {"tool_call_id": "", "name": ""}),
    (None, {"tool_call_id": " ", "name": " "}),
    (None, {}),
    ({"tool_call_id": "wrong_id", "name": "read_file"}, {"name": "read_file"}),
    ({"tool_call_id": "", "name": "unknown_tool"}, {"name": "read_file"}),
])
def test_legacy_tool_results_recover_ids_without_consuming_unmatched_calls(unmatched_fields, result_fields):
    from engine.inference.tool_format import build_llm_messages, repair_tool_result_pairing_with_stats

    messages = [{"role": "assistant", "content": "", "_meta": {"tool_mode": "native"},
                 "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {}}]}]
    if unmatched_fields is not None:
        messages.append({"role": "tool", "content": "WRONG_RESULT", **unmatched_fields})
    messages.append({"role": "tool", "content": "ACTUAL_RESULT", "_meta": {"tool_mode": "native"}, **result_fields})
    repaired, stats = repair_tool_result_pairing_with_stats(messages)
    assert stats["removed_orphan_tool_results"] == int(unmatched_fields is not None)
    assert stats["fixed_orphan_tool_calls"] == 0
    for view in (repaired, build_llm_messages(messages, NativeToolFormatter())):
        assert view[0]["tool_calls"][0]["id"] == "c1"
        assert [(m["tool_call_id"], m["name"], m["content"]) for m in view[1:]] == [
            ("c1", "read_file", "ACTUAL_RESULT"),
        ]


@pytest.mark.parametrize("positional", [False, True])
def test_tool_result_pairing_prefers_names_and_consumes_each_call_once(positional):
    from engine.inference.tool_format import repair_tool_result_pairing_with_stats

    messages = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "name": "read_file", "arguments": {}},
        {"id": "c2", "name": "search", "arguments": {}},
    ]}]
    if positional:
        messages += [
            {"role": "tool", "tool_call_id": "", "name": "", "content": "FIRST"},
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "DUPLICATE"},
            {"role": "tool", "content": "SECOND"},
        ]
        expected = [("c1", "read_file", "FIRST"), ("c2", "search", "SECOND")]
    else:
        messages += [
            {"role": "tool", "name": "search", "content": "SEARCH_RESULT"},
            {"role": "tool", "name": "read_file", "content": "READ_RESULT"},
        ]
        expected = [("c1", "read_file", "READ_RESULT"), ("c2", "search", "SEARCH_RESULT")]
    repaired, stats = repair_tool_result_pairing_with_stats(messages)
    assert stats["removed_orphan_tool_results"] == int(positional)
    assert [(m["tool_call_id"], m["name"], m["content"]) for m in repaired[1:]] == expected


def test_anthropic_finish_result_merges_next_turn_text_without_continued():
    from providers.anthropic import _convert_messages

    messages = [
        {"role": "user", "content": "I1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "f1", "type": "function",
             "function": {"name": "finish", "arguments": '{"text":"DONE"}'}},
        ]},
        {"role": "tool", "tool_call_id": "f1", "name": "finish", "content": "ok"},
        {"role": "user", "content": "D2", "_dynamic": True},
        {"role": "user", "content": "I2"},
    ]
    _, converted = _convert_messages(messages)
    assert [m["role"] for m in converted] == ["user", "assistant", "user"]
    assert converted[-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "f1", "content": "ok"},
        {"type": "text", "text": "D2"},
        {"type": "text", "text": "I2"},
    ]
    assert not any(block.get("text") == "(continued)" for m in converted for block in m["content"])


def test_anthropic_prevents_tool_result_after_merged_text_block():
    from providers.anthropic import _convert_messages

    _, converted = _convert_messages([
        {"role": "tool", "tool_call_id": "first", "content": "result one"},
        {"role": "user", "content": "ordinary text"},
        {"role": "tool", "tool_call_id": "second", "content": "result two"},
    ])
    assert converted == [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "first", "content": "result one"},
            {"type": "text", "text": "ordinary text"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "(continued)"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "second", "content": "result two"},
        ]},
    ]


def test_gemini_json_mode_history_rebuilds_function_call_when_switched_to_native():
    from providers.gemini import GeminiProvider

    messages = [
        {"role": "user", "content": "Read README"},
        {
            "role": "assistant",
            "content": "Checking README",
            "tool_calls": [
                {"id": "tc_json_1", "name": "read_file", "arguments": {"path": "README.md"}},
            ],
            "_meta": {
                "tool_mode": "json",
                "metadata": {
                    "gemini": {
                        "raw_parts": [
                            {"text": "Checking README\n<<<TOOL_CALL>>>{\"name\":\"read_file\"}<<<END_TOOL_CALL>>>"},
                        ],
                    },
                },
            },
        },
        {
            "role": "tool",
            "tool_call_id": "tc_json_1",
            "name": "read_file",
            "content": "{\"ok\": true}",
        },
        {"role": "user", "content": "Continue"},
    ]
    _, contents = GeminiProvider._convert_messages(messages)
    assert contents[1]["parts"] == [
        {"text": "Checking README"},
        {
            "functionCall": {"name": "read_file", "args": {"path": "README.md"}},
            "thoughtSignature": "skip_thought_signature_validator",
        },
    ]
    assert contents[2]["parts"] == [
        {"functionResponse": {"name": "read_file", "response": {"ok": True}}},
    ]


@pytest.fixture
def branch_anchor_state(tmp_path):
    from engine.conversation_store import ConversationStore, Message
    from supervisor.eventlog import EventLog
    from supervisor.policy import PolicyEngine
    from supervisor.state import SupervisorState

    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "events.jsonl", run_id="branch-anchor"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    parent = state.get_or_create_session(channel="discord_guild", conversation_key="discord:anchor-test")
    store = ConversationStore(tmp_path / "data" / "conversations")
    history = [Message(id="system-history", role="system", content="stored system")]
    history.extend(
        Message(id=f"history-{i}", role="user", content=f"past task {i}", source_task_id=f"history-task-{i}")
        for i in range(5)
    )
    store.append_batch(parent, history)
    return state, parent, history


def _anchor_store(state):
    from engine.conversation_store import ConversationStore
    return ConversationStore(state.workspace_root / "data" / "conversations")


def _anchor_task(state, parent, branch, fork_meta):
    from supervisor.types import Task, TaskKind, TaskStatus
    now = datetime.now(timezone.utc)
    task = Task(
        task_id="anchor-entry", session_id=branch, kind=TaskKind.node, node_id="entry",
        status=TaskStatus.running, created_at=now, updated_at=now,
        input={"parent_session_id": parent, "branch_session_id": branch,
               "base_count": fork_meta["base_count"], "base_last_id": fork_meta["base_last_id"]},
    )
    state.tasks[task.task_id] = task
    return task


def _anchor_delta():
    from engine.conversation_store import Message
    return [
        Message(id="turn-user", role="user", source_task_id="anchor-entry",
                content=[{"type": "text", "text": "current input"},
                         {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}]),
        Message(id="turn-assistant", role="assistant", content="checking", source_task_id="anchor-entry",
                meta={"provider": "test", "metadata": {"signature": "preserve"}},
                tool_calls=[{"id": "call-current", "type": "function", "function": {"name": "search", "arguments": "{}"}}]),
        Message(id="turn-tool", role="tool", content="found", source_task_id="anchor-entry",
                tool_call_id="call-current", name="search"),
    ]


def test_entry_branch_fork_copies_complete_history_and_anchor(branch_anchor_state):
    state, parent, history = branch_anchor_state
    branch, meta = state._create_entry_branch_locked(parent, 901)
    assert meta == {"copied": len(history), "base_count": len(history), "base_last_id": history[-1].id}
    assert [m.to_dict() for m in _anchor_store(state).load(branch)] == [m.to_dict() for m in history]


@pytest.mark.parametrize("shorten_prefix", [False, True])
def test_branch_anchor_merge_preserves_all_new_messages(branch_anchor_state, shorten_prefix):
    state, parent, history = branch_anchor_state
    branch, meta = state._create_entry_branch_locked(parent, 902)
    delta = _anchor_delta()
    store = _anchor_store(state)
    store.append_batch(branch, delta)
    if shorten_prefix:
        # The anchor becomes index zero, while the old base_count exceeds file length.
        store.replace_all(branch, [history[-1]] + delta)
    count = state._merge_branch_locked(parent, branch, meta["base_count"], base_last_id=meta["base_last_id"])
    assert count == len(delta)
    assert [m.to_dict() for m in _anchor_store(state).load(parent)] == [m.to_dict() for m in history + delta]


@pytest.mark.parametrize("anchor", ["", "not-present"])
def test_branch_anchor_merge_retains_count_fallback(branch_anchor_state, anchor):
    state, parent, history = branch_anchor_state
    branch, meta = state._create_entry_branch_locked(parent, 903)
    delta = _anchor_delta()
    _anchor_store(state).append_batch(branch, delta)
    assert state._merge_branch_locked(parent, branch, meta["base_count"], base_last_id=anchor) == len(delta)
    assert [m.id for m in _anchor_store(state).load(parent)] == [m.id for m in history + delta]


@pytest.mark.parametrize("shorten_prefix", [False, True])
def test_branch_compact_sync_uses_exact_parent_prefix(branch_anchor_state, shorten_prefix):
    from engine.conversation_store import Message
    state, parent, history = branch_anchor_state
    branch, meta = state._create_entry_branch_locked(parent, 904)
    task = _anchor_task(state, parent, branch, meta)
    delta = _anchor_delta()
    store = _anchor_store(state)
    store.append_batch(branch, delta)
    if shorten_prefix:
        store.replace_all(branch, [history[-1]] + delta)
    # A parent-retained record absent from the fork detects count-only prefix reconstruction.
    new_prefix = [
        Message(id="canonical-summary", role="user", content="compact summary", message_type="summary",
                source_task_id="compact_summary", meta={"compressed_task_ids": ["history-task-0"]}),
        Message(id="parent-other-branch", role="assistant", content="another committed turn", source_task_id="other-entry"),
    ]
    store.replace_all(parent, new_prefix)
    state._sync_compact_to_branches(parent, new_prefix)
    branch_messages = _anchor_store(state).load(branch)
    assert [m.to_dict() for m in branch_messages[:len(new_prefix)]] == [m.to_dict() for m in _anchor_store(state).load(parent)]
    assert [m.to_dict() for m in branch_messages[len(new_prefix):]] == [m.to_dict() for m in delta]
    assert task.input["base_count"] == len(new_prefix)
    assert task.input["base_last_id"] == new_prefix[-1].id
    assert state._merge_branch_locked(parent, branch, task.input["base_count"], base_last_id=task.input["base_last_id"]) == len(delta)
    assert [m.to_dict() for m in _anchor_store(state).load(parent)] == [m.to_dict() for m in new_prefix + delta]


def test_parent_compact_then_branch_finalize_preserves_tail(branch_anchor_state):
    from supervisor.types import TaskStatus
    state, parent, _ = branch_anchor_state
    branch, meta = state._create_entry_branch_locked(parent, 905)
    task = _anchor_task(state, parent, branch, meta)
    delta = _anchor_delta()
    _anchor_store(state).append_batch(branch, delta)
    result = state._apply_compact_via_conv_store_locked(parent, "retained history", keep_recent=1, threshold_tokens=0)
    assert result["after"] < result["before"]
    prefix = list(_anchor_store(state).load(parent))
    synced = _anchor_store(state).load(branch)
    assert [m.to_dict() for m in synced[:len(prefix)]] == [m.to_dict() for m in prefix]
    assert task.input["base_count"] == len(prefix) and task.input["base_last_id"] == prefix[-1].id
    task.status = TaskStatus.completed
    assert state._finalize_branch_task_locked(task, merge=True) == parent
    assert [m.to_dict() for m in _anchor_store(state).load(parent)] == [m.to_dict() for m in prefix + delta]
    assert branch not in state.sessions and task.input["_branch_finalized"]


def test_branch_checkpoint_advances_anchor_before_finalize(branch_anchor_state):
    from engine.conversation_store import Message
    from supervisor.types import TaskStatus
    state, parent, history = branch_anchor_state
    branch, meta = state._create_entry_branch_locked(parent, 906)
    task = _anchor_task(state, parent, branch, meta)
    delta = _anchor_delta()
    _anchor_store(state).replace_all(branch, [history[-1]] + delta)
    assert state._checkpoint_branch_task_locked(task) == len(delta)
    assert task.input["base_count"] == meta["base_count"] + len(delta)
    assert task.input["base_last_id"] == delta[-1].id
    assert state._checkpoint_branch_task_locked(task) == 0
    late = Message(id="after-checkpoint", role="assistant", content="late", source_task_id=task.task_id)
    _anchor_store(state).append(branch, late)
    assert state._checkpoint_branch_task_locked(task) == 1
    assert task.input["base_last_id"] == late.id
    final = Message(id="after-second-checkpoint", role="assistant", content="final", source_task_id=task.task_id)
    _anchor_store(state).append(branch, final)
    task.status = TaskStatus.completed
    assert state._finalize_branch_task_locked(task, merge=True) == parent
    merged = _anchor_store(state).load(parent)
    assert [m.id for m in merged] == [m.id for m in history + delta + [late, final]]
    assert len({m.id for m in merged}) == len(merged)


def test_branch_compact_sync_skips_unrecoverable_anchor(branch_anchor_state):
    from engine.conversation_store import Message
    state, parent, _ = branch_anchor_state
    branch, meta = state._create_entry_branch_locked(parent, 907)
    task = _anchor_task(state, parent, branch, meta)
    shortened = [Message(id="replacement-without-anchor", role="user", content="unknown prefix")]
    store = _anchor_store(state)
    store.replace_all(branch, shortened)
    prefix = [Message(id="parent-summary", role="user", content="summary")]
    state._sync_entry_branch_compact_locked(store, branch, prefix, base_count=meta["base_count"], tasks=[task])
    assert [m.to_dict() for m in _anchor_store(state).load(branch)] == [m.to_dict() for m in shortened]
    assert task.input["base_last_id"] == meta["base_last_id"]


def test_branch_merge_io_failure_does_not_finalize(branch_anchor_state, monkeypatch):
    from engine.conversation_store import ConversationStore
    from supervisor.types import TaskStatus
    state, parent, _ = branch_anchor_state
    branch, meta = state._create_entry_branch_locked(parent, 908)
    task = _anchor_task(state, parent, branch, meta)
    _anchor_store(state).append_batch(branch, _anchor_delta())

    def fail_append(self, session_id, messages):
        raise OSError("parent append failed")

    monkeypatch.setattr(ConversationStore, "append_batch", fail_append)
    assert state._merge_branch_locked(parent, branch, meta["base_count"], base_last_id=meta["base_last_id"]) is None
    task.status = TaskStatus.completed
    assert state._finalize_branch_task_locked(task, merge=True) == parent
    assert not task.input.get("_branch_finalized") and branch in state.sessions


def test_empty_parent_branch_sync_keeps_current_turn(branch_anchor_state):
    from engine.conversation_store import Message
    state, _, _ = branch_anchor_state
    parent = state.get_or_create_session(channel="discord_guild", conversation_key="discord:empty-anchor")
    branch, meta = state._create_entry_branch_locked(parent, 909)
    assert meta == {"copied": 0, "base_count": 0, "base_last_id": ""}
    assert _anchor_store(state).load(branch) == []
    task = _anchor_task(state, parent, branch, meta)
    delta = _anchor_delta()
    _anchor_store(state).append_batch(branch, delta)
    prefix = [Message(id="new-parent-prefix", role="user", content="parent summary", message_type="summary")]
    _anchor_store(state).replace_all(parent, prefix)
    state._sync_compact_to_branches(parent, prefix)
    assert [m.to_dict() for m in _anchor_store(state).load(branch)] == [m.to_dict() for m in prefix + delta]
    assert task.input["base_count"] == 1 and task.input["base_last_id"] == prefix[0].id


@pytest.fixture(params=["PNG", "JPEG"])
def prepared_image_message(request: pytest.FixtureRequest, tmp_path: Path) -> dict:
    import io
    from PIL import Image
    from engine.attachments import build_multimodal_content, prepare_messages_for_llm, save_attachment

    image_bytes = io.BytesIO()
    with Image.new("RGB", (24, 12), "red") as image:
        image.save(image_bytes, format=request.param)
    suffix = request.param.lower()
    attachment = save_attachment(
        tmp_path,
        "native-vision",
        image_bytes.getvalue(),
        filename=f"sample.{suffix}",
        mime_type=f"image/{suffix}",
    )
    content = build_multimodal_content("Describe this image", [attachment], workspace_root=tmp_path)
    messages = prepare_messages_for_llm([{"role": "user", "content": content}], tmp_path)
    return messages[0]


@pytest.mark.parametrize("provider_name", ["openai", "openai-responses", "anthropic", "gemini-aistudio"])
def test_uploaded_image_reaches_native_provider_payload(prepared_image_message: dict, provider_name: str) -> None:
    import base64
    import io
    from PIL import Image
    from providers.anthropic import AnthropicProvider
    from providers.gemini import GeminiAIStudioProvider
    from providers.openai import OpenAIProvider
    from providers.openai_responses import OpenAIResponsesProvider

    messages = [prepared_image_message]
    options = {"http": None, "api_key": "test", "base_url": None, "model": "test"}
    if provider_name == "openai":
        payload = OpenAIProvider(**options)._build_payload(messages, None)
        image_part = next(part for part in payload["messages"][0]["content"] if part["type"] == "image_url")
        header, encoded = image_part["image_url"]["url"].split(",", 1)
        assert header == "data:image/jpeg;base64"
    elif provider_name == "openai-responses":
        provider = OpenAIResponsesProvider(**options)
        instructions, input_items = provider._convert_messages(messages)
        payload = provider._build_payload(input_items=input_items, instructions=instructions, tools=None, stream=False)
        image_part = next(part for part in payload["input"][0]["content"] if part["type"] == "input_image")
        header, encoded = image_part["image_url"].split(",", 1)
        assert header == "data:image/jpeg;base64"
    elif provider_name == "anthropic":
        payload = AnthropicProvider(**options)._build_payload(messages)
        image_part = next(part for part in payload["messages"][0]["content"] if part["type"] == "image")
        assert image_part["source"]["type"] == "base64"
        assert image_part["source"]["media_type"] == "image/jpeg"
        encoded = image_part["source"]["data"]
    else:
        payload = GeminiAIStudioProvider(**options)._build_body(messages, None)
        image_part = next(part for part in payload["contents"][0]["parts"] if "inlineData" in part)
        assert image_part["inlineData"]["mimeType"] == "image/jpeg"
        encoded = image_part["inlineData"]["data"]
    with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as image:
        assert image.format == "JPEG"
        assert image.size == (24, 12)



@pytest.mark.parametrize("use_flag", [True, False])
def test_same_task_preempt_subsegments_preserve_tool_rounds(use_flag):
    def msg(role, text, **fields):
        meta = {"source_task_id": "one-task"}
        if fields.pop("preempt", False):
            if use_flag:
                meta["preempt"] = True
            else:
                fields["message_type"] = "user_input"
        return {"role": role, "content": text, "_meta": meta, **fields}

    messages = [
        msg("user", "initial"), msg("assistant", "completed first turn"),
        msg("user", "second", preempt=True),
        msg("assistant", "", tool_calls=[{"id": "call", "function": {"name": "read_file"}}]),
        msg("user", "arrived during tool call", preempt=True),
        msg("tool", "file contents", tool_call_id="call"),
        msg("assistant", "completed second turn"),
        msg("user", "third", preempt=True), msg("assistant", "final"),
    ]
    prefix, segments = compact.split_task_segments(messages)
    assert prefix == []
    assert [len(seg) for seg in segments] == [2, 5, 2]
    assert [m for seg in segments for m in seg] == messages
    assert compact.count_real_task_segments(messages) == 3
    # Multiple inbound user messages alone are not completed turns.
    assert len(compact.split_task_segments([messages[0], messages[2]])[1]) == 1


def test_compact_token_estimates_use_completion_and_cjk():
    assert compact.estimate_message_tokens({"role": "assistant", "content": "中" * 20}) == 10
    assert compact.estimate_message_tokens({"role": "assistant", "content": "short", "_meta": {
        "usage": {"completion_tokens": 900},
    }}) == 900
    assert compact.estimate_message_tokens({"role": "assistant", "content": "a" * 40, "_meta": {
        "usage": {"completion_tokens": True},
    }}) == 20
    assert compact.estimate_message_tokens({"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "image"}},
    ]}) == 1600


def test_dream_hit_cache_prunes_orphans_even_with_empty_topology(tmp_path):
    import json
    from engine.builtin.dream import DreamHandler

    path = tmp_path / "data" / "memory" / ".hit_cache.json"
    path.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps({"live": now, "orphan": now}), encoding="utf-8")
    handler = DreamHandler()
    assert json.loads(handler._load_hit_cache_json(tmp_path, valid_ids={"live"})) == {"live": now}
    assert json.loads(path.read_text(encoding="utf-8")) == {"live": now}
    assert json.loads(handler._load_hit_cache_json(tmp_path, valid_ids=set())) == {}
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_builtin_node_fallback_outside_repository(tmp_path):
    from engine.node import load_node

    node = load_node(tmp_path, "system.memory_extractor")
    assert node is not None
    assert "manage_memory" in node.tool_access.allow



def test_memory_extractor_scans_source_namespace_and_legacy_root(tmp_path):
    from engine.builtin.memory_extract import MemoryExtractHandler

    mem_root = tmp_path / "data" / "memory"
    (mem_root / "source").mkdir(parents=True)
    (mem_root / "other").mkdir()
    for path in [mem_root / "shared.yaml", mem_root / "source" / "private.yaml",
                 mem_root / "source" / "shared.yaml", mem_root / "other" / "hidden.yaml"]:
        path.write_text("entries: []", encoding="utf-8")
    handler = MemoryExtractHandler()
    handler._memory_extract_pending["session"] = {
        "transcript": "user preference", "task_count": 1, "_source_node_id": "source",
    }
    created = []
    handler._fire_memory_extract_idle({"workspace_root": tmp_path, "acquire_lock": RLock(),
        "create_task": lambda **kwargs: created.append(kwargs)}, "session")
    assert len(created) == 1
    instruction = created[0]["input_data"]["instruction"]
    assert "private, shared" in instruction
    assert "hidden" not in instruction
    assert created[0]["input_data"]["_memory_route_ns"] == "source"


@pytest.mark.parametrize("message_type", ["dispatch_result", "async_tool_result"])
def test_memory_extractor_skips_internal_result_turns(message_type):
    from engine.builtin.memory_extract import MemoryExtractHandler

    handler = MemoryExtractHandler()
    task = SimpleNamespace(result={"action": "finish"}, kind="node", input={"inbound_message_type": message_type})
    # No routing/runtime callbacks are available: internal result turns must exit at the gate.
    handler._maybe_trigger_memory_extract_locked(ctx={}, task=task)
    assert not handler._memory_extract_pending
    assert not handler._memory_extract_task_counts
