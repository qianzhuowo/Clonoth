"""多轮对话提示词构建与 context 连续性验证。

用法：python scripts/test_multi_turn.py

不需要真实 LLM 或 Supervisor，全部用 mock / 本地数据。
验证内容：
  1. 第一轮：从零构建 system prompt + skill + history + user message
  2. 第二轮：从 context_ref 恢复历史，重建 system prompt，追加新 user message
  3. 第三轮：模拟 dispatch→resume，从快照恢复 + 注入 resume_data
  4. 工具列表注入正确性（真实工具 + 伪工具 finish/ask/dispatch_node）
  5. %%DYNAMIC%% 分段正确性
  6. context_ref 快照读写正确性
  7. _strip_trailing_pseudo_call 对上一轮 finish 的处理
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

os.environ.setdefault("OPENAI_API_KEY", "mock-key")
os.environ.setdefault("OPENAI_MODEL", "mock-model")

from engine.node import load_node
from engine.prompt import assemble_prompt
from engine.context_store import (
    save_context_snapshot,
    load_context_snapshot,
    write_context_snapshot,
)
from engine.runner import _strip_trailing_pseudo_call
from engine.ai_step import (
    _build_resume_messages,
    _filter_tool_specs,
    _to_openai_tools,
    _finish_spec,
    _ask_spec,
    _dispatch_node_spec,
)
from toolbox.registry import ToolRegistry
from toolbox.skills_runtime import build_skill_messages


# ============================================================
#  工具函数
# ============================================================

def _count_roles(msgs: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in msgs:
        r = m.get("role", "?")
        counts[r] = counts.get(r, 0) + 1
    return counts


def _find_tool_names(openai_tools: list[dict]) -> set[str]:
    return {t.get("function", {}).get("name", "") for t in openai_tools}


# ============================================================
#  Test 1: 第一轮从零构建
# ============================================================

def test_turn1_fresh_build() -> dict[str, Any]:
    """模拟第一轮对话：无 context_ref，无 history。
    验证 system prompt 构建、变量替换、skill 注入、工具列表。
    """
    print("\n[test] Turn 1: 从零构建")

    node = load_node(WORKSPACE, "bootstrap.shell_orchestrator")
    assert node is not None

    instruction = "帮我看看项目结构"

    # 1. 组装 system prompt
    prompt_vars = {
        "node_id": node.id,
        "node_name": node.name,
        "instruction": instruction,
    }
    system_msgs = assemble_prompt(WORKSPACE, node, variables=prompt_vars)
    assert len(system_msgs) >= 1
    prompt_text = "\n".join(m["content"] for m in system_msgs)

    # 变量已替换
    assert "{{include:" not in prompt_text, "include 未展开"
    assert "{{node_id}}" not in prompt_text, "node_id 未替换"
    assert "{{instruction}}" not in prompt_text, "instruction 未替换"
    assert instruction in prompt_text, "instruction 内容未出现在 prompt 中"
    assert node.id in prompt_text, "node_id 值未出现在 prompt 中"

    # _shared.md 内容已内联
    assert "Substrate Boundary" in prompt_text, "_shared.md 未被 include"
    assert "Non-Fabrication" in prompt_text, "_shared.md 未被 include"

    print(f"  system prompt: {len(prompt_text)} chars, {len(system_msgs)} msg(s)")

    # 2. Skill 注入（orchestrator 的 skills.mode=none，应该为空）
    skill_msgs = build_skill_messages(
        WORKSPACE,
        instruction_text=instruction,
        history=[],
        skill_mode=node.skill_access.mode,
        skill_allow=node.skill_access.allow,
    )
    assert skill_msgs == [], f"orchestrator skills.mode=none，应返回空，实际 {len(skill_msgs)} 条"
    print("  skill injection: 0 (mode=none, correct)")

    # 3. 构建完整 messages 列表
    history: list[dict] = []  # 第一轮无历史
    messages = list(system_msgs)
    messages.extend(skill_msgs)
    messages.extend(history)
    messages.append({"role": "user", "content": instruction})

    roles = _count_roles(messages)
    assert roles.get("system", 0) >= 1
    assert roles.get("user", 0) == 1
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == instruction
    print(f"  messages: {len(messages)} total, roles={roles}")

    # 4. 工具列表
    registry = ToolRegistry(workspace_root=WORKSPACE, tools_dir=WORKSPACE / "tools")
    tool_specs = _filter_tool_specs(node, registry.list_specs())
    openai_tools = _to_openai_tools(tool_specs) if tool_specs else []

    # 伪工具
    delegate_targets = list(node.delegate_targets)
    if delegate_targets:
        openai_tools.append(_dispatch_node_spec(delegate_targets))
    openai_tools.append(_finish_spec())
    openai_tools.append(_ask_spec())

    tool_names = _find_tool_names(openai_tools)
    assert "finish" in tool_names
    assert "ask" in tool_names
    assert "dispatch_node" in tool_names, "orchestrator 有 delegate_targets，应有 dispatch_node"

    # orchestrator 的 allowlist 工具
    for allowed in node.tool_access.allow:
        assert allowed in tool_names, f"allowlist 工具 {allowed} 未出现"

    print(f"  tools: {len(openai_tools)} total, names={sorted(tool_names)}")

    # 5. 保存快照（模拟 ai_step 执行后的持久化）
    # 追加一条 assistant 回复模拟 finish
    messages.append({
        "role": "assistant",
        "content": 'Calling tool: finish({"text": "这是项目结构的回复", "summary": "done"})'
    })
    context_ref = save_context_snapshot(
        WORKSPACE, "test-multi-turn-session",
        {"version": 1, "node_id": node.id, "messages": messages, "step_count": 1},
        context_id="turn1",
    )
    assert context_ref
    print(f"  context_ref saved: {context_ref}")

    return {
        "context_ref": context_ref,
        "node_id": node.id,
        "messages": messages,
    }


# ============================================================
#  Test 2: 第二轮从 context_ref 恢复
# ============================================================

def test_turn2_context_resume(turn1: dict[str, Any]) -> dict[str, Any]:
    """模拟第二轮对话：有 context_ref，非 resume。
    验证从快照加载历史、剥离伪工具调用、重建 system prompt。
    """
    print("\n[test] Turn 2: 从 context_ref 恢复")

    context_ref = turn1["context_ref"]
    node_id = turn1["node_id"]
    node = load_node(WORKSPACE, node_id)
    assert node is not None

    instruction = "读取 README.md 的内容"

    # 1. 加载快照
    snapshot = load_context_snapshot(WORKSPACE, context_ref)
    assert snapshot is not None
    assert isinstance(snapshot.get("messages"), list)
    print(f"  snapshot loaded: {len(snapshot['messages'])} messages")

    # 2. 提取非系统消息作为 history（模拟 runner._run_node_task 的逻辑）
    history = [m for m in snapshot["messages"] if m.get("role") != "system"]
    assert len(history) >= 2, "至少有 user + assistant"
    print(f"  history (non-system): {len(history)} messages")

    # 3. 剥离尾部伪工具调用
    history_stripped = _strip_trailing_pseudo_call(history)

    # finish 的 text 应该被提取为正常 assistant 内容
    last = history_stripped[-1]
    assert last["role"] == "assistant"
    assert "这是项目结构的回复" in last["content"], "finish text 应被提取"
    assert "Calling tool:" not in last["content"], "伪工具标记应被剥离"
    print(f"  stripped last assistant: {last['content'][:60]}...")

    # 4. 重建 system prompt（context_ref 被清空，走 else 分支）
    prompt_vars = {
        "node_id": node.id,
        "node_name": node.name,
        "instruction": instruction,
    }
    system_msgs = assemble_prompt(WORKSPACE, node, variables=prompt_vars)

    # 5. 组装完整 messages
    messages = list(system_msgs)
    messages.extend(history_stripped)

    # 检查 instruction 去重：history_stripped 末尾不是当前 instruction
    _last = history_stripped[-1] if history_stripped else None
    _last_content = _last.get("content", "") if isinstance(_last, dict) else ""
    _already = (
        _last is not None
        and _last.get("role") == "user"
        and isinstance(_last_content, str)
        and _last_content.strip() == instruction.strip()
    )
    if not _already:
        messages.append({"role": "user", "content": instruction})

    roles = _count_roles(messages)
    assert roles.get("system", 0) >= 1
    assert roles.get("user", 0) >= 2, "应有 turn1 的 user + turn2 的 user"
    print(f"  messages: {len(messages)} total, roles={roles}")

    # 6. 验证 instruction 在新 prompt 中
    prompt_text = "\n".join(m["content"] for m in system_msgs)
    assert instruction in prompt_text, "turn2 instruction 应在 system prompt 中"

    # 7. 验证消息顺序
    # system(s) → history(user, assistant, ...) → user(turn2)
    first_non_system = None
    for i, m in enumerate(messages):
        if m["role"] != "system":
            first_non_system = i
            break
    assert first_non_system is not None
    # 所有 system 消息在前
    for m in messages[:first_non_system]:
        assert m["role"] == "system"
    # 最后一条是 turn2 的 user
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == instruction
    print("  message order: system(s) → history → user(turn2). OK")

    # 保存 turn2 快照
    messages.append({
        "role": "assistant",
        "content": 'Calling tool: dispatch_node({"target": "bootstrap.executor", "instruction": "读取 README.md"})'
    })
    context_ref2 = save_context_snapshot(
        WORKSPACE, "test-multi-turn-session",
        {"version": 1, "node_id": node.id, "messages": messages, "step_count": 1},
        context_id="turn2",
    )
    print(f"  context_ref saved: {context_ref2}")

    return {
        "context_ref": context_ref2,
        "node_id": node.id,
        "messages": messages,
    }


# ============================================================
#  Test 3: 第三轮 dispatch→resume
# ============================================================

def test_turn3_resume(turn2: dict[str, Any]) -> None:
    """模拟第三轮：从 dispatch 恢复，注入 resume_data。
    验证快照加载 + resume 消息构建。
    """
    print("\n[test] Turn 3: dispatch 恢复")

    context_ref = turn2["context_ref"]
    node_id = turn2["node_id"]

    # 1. 加载快照（模拟 resume 场景，context_ref 不被清空）
    snapshot = load_context_snapshot(WORKSPACE, context_ref)
    assert snapshot is not None
    messages = list(snapshot.get("messages") or [])
    print(f"  snapshot loaded: {len(messages)} messages")

    # 2. 构建 resume_data（模拟下游节点完成后 Supervisor 注入的数据）
    resume_data = {
        "type": "child_result",
        "child_node_id": "bootstrap.executor",
        "result": {
            "summary": "已读取 README.md",
            "text": "# Clonoth\n声明式、可审计的多 Agent 引擎...",
            "attachments": [],
        },
    }

    resume_msgs = _build_resume_messages(resume_data)
    assert len(resume_msgs) >= 1
    assert resume_msgs[0]["role"] == "user"
    content = resume_msgs[0]["content"]
    assert "bootstrap.executor" in content
    assert "已完成" in content
    assert "README" in content or "Clonoth" in content
    print(f"  resume message: {content[:80]}...")

    # 3. 追加到 messages
    messages.extend(resume_msgs)
    roles = _count_roles(messages)
    print(f"  messages after resume: {len(messages)} total, roles={roles}")

    # 最后一条应该是 resume 注入的 user 消息
    assert messages[-1]["role"] == "user"
    assert "已完成" in str(messages[-1]["content"])

    # 4. 测试其他 resume 类型
    # child_ask
    ask_msgs = _build_resume_messages({
        "type": "child_ask",
        "child_node_id": "bootstrap.executor",
        "result": {"text": "需要指定文件路径"},
    })
    assert ask_msgs and "需要指定文件路径" in str(ask_msgs[0]["content"])
    print("  child_ask resume: OK")

    # child_failed
    fail_msgs = _build_resume_messages({
        "type": "child_failed",
        "child_node_id": "bootstrap.executor",
        "error": "文件不存在",
    })
    assert fail_msgs and "文件不存在" in str(fail_msgs[0]["content"])
    print("  child_failed resume: OK")

    # child_cancelled
    cancel_msgs = _build_resume_messages({
        "type": "child_cancelled",
        "child_node_id": "bootstrap.executor",
    })
    assert cancel_msgs and "取消" in str(cancel_msgs[0]["content"])
    print("  child_cancelled resume: OK")

    # tool_results (v1 兼容)
    tr_msgs = _build_resume_messages({
        "type": "tool_results",
        "tool_results": [
            {"name": "list_dir", "raw_inline": "file1.py\nfile2.py"},
            {"name": "read_file", "raw_inline": "content here"},
        ],
    })
    assert len(tr_msgs) == 2, f"2 个工具结果应产生 2 条消息，实际 {len(tr_msgs)}"
    assert 'Tool result for "list_dir"' in tr_msgs[0]["content"]
    assert 'Tool result for "read_file"' in tr_msgs[1]["content"]
    print("  tool_results (v1) resume: OK")


# ============================================================
#  Test 4: %%DYNAMIC%% 分段
# ============================================================

def test_dynamic_split() -> None:
    """验证 coder 节点的 %%DYNAMIC%% 分段。"""
    print("\n[test] %%DYNAMIC%% 分段")

    node = load_node(WORKSPACE, "bootstrap.coder")
    assert node is not None

    prompt_msgs = assemble_prompt(WORKSPACE, node, variables={
        "instruction": "写一段代码",
    })

    assert len(prompt_msgs) == 2, f"coder 有 %%DYNAMIC%%，应产生 2 条 system 消息，实际 {len(prompt_msgs)}"
    assert prompt_msgs[0]["role"] == "system"
    assert prompt_msgs[1]["role"] == "system"

    static_part = prompt_msgs[0]["content"]
    dynamic_part = prompt_msgs[1]["content"]

    # 静态段不含动态变量
    assert "{{now}}" not in static_part
    # 动态段包含时间相关内容
    assert "当前时间" in dynamic_part
    # %%DYNAMIC%% 标记本身不出现在输出中
    assert "%%DYNAMIC%%" not in static_part
    assert "%%DYNAMIC%%" not in dynamic_part

    print(f"  static: {len(static_part)} chars")
    print(f"  dynamic: {len(dynamic_part)} chars")
    print("  OK")


# ============================================================
#  Test 5: executor 节点工具列表
# ============================================================

def test_executor_tools() -> None:
    """验证 executor 节点的工具列表：mode=all, deny=[execute_command]。"""
    print("\n[test] executor 工具列表")

    node = load_node(WORKSPACE, "bootstrap.executor")
    assert node is not None

    registry = ToolRegistry(workspace_root=WORKSPACE, tools_dir=WORKSPACE / "tools")
    tool_specs = _filter_tool_specs(node, registry.list_specs())
    openai_tools = _to_openai_tools(tool_specs) if tool_specs else []

    # 加上伪工具
    delegate_targets = list(node.delegate_targets)
    if delegate_targets:
        openai_tools.append(_dispatch_node_spec(delegate_targets))
    openai_tools.append(_finish_spec())
    openai_tools.append(_ask_spec())

    tool_names = _find_tool_names(openai_tools)

    # execute_command 在 deny 列表中
    assert "execute_command" not in tool_names, "executor deny execute_command"
    # 但其他内置工具应该存在
    assert "list_dir" in tool_names
    assert "read_file" in tool_names
    assert "write_file" in tool_names
    assert "search_in_files" in tool_names
    # 伪工具
    assert "finish" in tool_names
    assert "ask" in tool_names
    # executor 有 delegate_targets
    assert "dispatch_node" in tool_names

    print(f"  tools: {len(openai_tools)}, denied: execute_command")
    print(f"  names: {sorted(tool_names)}")
    print("  OK")


# ============================================================
#  Test 6: executor 的 skill 注入（mode=all）
# ============================================================

def test_executor_skills() -> None:
    """验证 executor 节点的 skill 注入（与 orchestrator 的 none 对比）。"""
    print("\n[test] executor skill 注入")

    # executor 没有显式 skills 配置，默认 mode=all
    node = load_node(WORKSPACE, "bootstrap.executor")
    assert node is not None

    # 根据实际 skills/ 目录是否有文件来判断
    skill_msgs = build_skill_messages(
        WORKSPACE,
        instruction_text="读取文件",
        history=[],
        skill_mode=node.skill_access.mode,
        skill_allow=node.skill_access.allow,
    )
    # 如果 skills/ 下没有文件，返回空也是正确的
    skills_dir = WORKSPACE / "skills"
    has_skills = skills_dir.exists() and any(skills_dir.glob("*/SKILL.md"))
    if has_skills:
        assert len(skill_msgs) >= 1, "有 skills 文件但未注入"
        print(f"  skill messages: {len(skill_msgs)} (skills exist)")
    else:
        assert skill_msgs == []
        print("  skill messages: 0 (no skills on disk)")

    # coder 也是 mode=all
    coder = load_node(WORKSPACE, "bootstrap.coder")
    assert coder is not None
    assert coder.skill_access.mode == "all"
    print("  coder skill mode: all. OK")

    # orchestrator 是 mode=none
    orch = load_node(WORKSPACE, "bootstrap.shell_orchestrator")
    assert orch is not None
    assert orch.skill_access.mode == "none"
    print("  orchestrator skill mode: none. OK")


# ============================================================
#  Test 7: _strip_trailing_pseudo_call 各种情况
# ============================================================

def test_strip_pseudo_call() -> None:
    """验证 _strip_trailing_pseudo_call 对不同伪工具的处理。"""
    print("\n[test] _strip_trailing_pseudo_call")

    # finish: 提取 text
    h1 = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": 'Calling tool: finish({"text": "你好", "summary": "greeting"})'},
    ]
    r1 = _strip_trailing_pseudo_call(h1)
    assert r1[-1]["role"] == "assistant"
    assert "你好" in r1[-1]["content"]
    assert "Calling tool" not in r1[-1]["content"]
    print("  finish → extracted text. OK")

    # dispatch_node: 删除
    h2 = [
        {"role": "user", "content": "do something"},
        {"role": "assistant", "content": 'Calling tool: dispatch_node({"target": "x", "instruction": "y"})'},
    ]
    r2 = _strip_trailing_pseudo_call(h2)
    assert len(r2) == 1, "dispatch_node 调用应被删除"
    assert r2[0]["role"] == "user"
    print("  dispatch_node → removed. OK")

    # ask: 删除
    h3 = [
        {"role": "user", "content": "do something"},
        {"role": "assistant", "content": 'Calling tool: ask({"text": "需要什么？"})'},
    ]
    r3 = _strip_trailing_pseudo_call(h3)
    assert len(r3) == 1
    print("  ask → removed. OK")

    # 有前缀文本的 finish: 保留前缀 + 提取 text
    h4 = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": '思考一下...\nCalling tool: finish({"text": "结果"})'},
    ]
    r4 = _strip_trailing_pseudo_call(h4)
    assert "结果" in r4[-1]["content"]
    assert "思考一下" in r4[-1]["content"]
    print("  finish with prefix → combined. OK")

    # 非伪工具调用: 不变
    h5 = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "正常回复"},
    ]
    r5 = _strip_trailing_pseudo_call(h5)
    assert r5 == h5
    print("  normal reply → unchanged. OK")

    # 空历史: 不变
    assert _strip_trailing_pseudo_call([]) == []
    print("  empty → unchanged. OK")


# ============================================================
#  Test 8: context_store 读写一致性
# ============================================================

def test_context_store_roundtrip() -> None:
    """验证 context_store 的保存、加载、覆写一致性。"""
    print("\n[test] context_store 读写")

    session_id = "test-roundtrip"
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
    ]

    # 保存
    ref = save_context_snapshot(
        WORKSPACE, session_id,
        {"version": 1, "node_id": "test", "messages": messages, "step_count": 1},
        context_id="roundtrip-1",
    )
    assert ref
    print(f"  saved: {ref}")

    # 加载
    loaded = load_context_snapshot(WORKSPACE, ref)
    assert loaded is not None
    assert loaded["messages"] == messages
    assert loaded["step_count"] == 1
    print("  loaded: messages match. OK")

    # 覆写
    messages.append({"role": "user", "content": "第二轮"})
    write_context_snapshot(WORKSPACE, ref, {
        "version": 1, "node_id": "test", "messages": messages, "step_count": 2,
    })
    loaded2 = load_context_snapshot(WORKSPACE, ref)
    assert loaded2 is not None
    assert len(loaded2["messages"]) == 4
    assert loaded2["step_count"] == 2
    print("  overwritten: 4 messages, step_count=2. OK")

    # 加载不存在的 ref
    assert load_context_snapshot(WORKSPACE, "") is None
    assert load_context_snapshot(WORKSPACE, "data/node_contexts/nonexist/x.json") is None
    print("  missing ref → None. OK")


# ============================================================
#  清理
# ============================================================

def cleanup() -> None:
    """清理测试生成的临时快照。"""
    for sid in ["test-multi-turn-session", "test-roundtrip"]:
        d = WORKSPACE / "data" / "node_contexts" / sid
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


# ============================================================
#  Main
# ============================================================

def main() -> None:
    print("=" * 60)
    print("Clonoth 多轮对话提示词构建与 context 连续性验证")
    print("=" * 60)

    try:
        turn1 = test_turn1_fresh_build()
        turn2 = test_turn2_context_resume(turn1)
        test_turn3_resume(turn2)
        test_dynamic_split()
        test_executor_tools()
        test_executor_skills()
        test_strip_pseudo_call()
        test_context_store_roundtrip()
    finally:
        cleanup()

    print("\n" + "=" * 60)
    print("ALL MULTI-TURN TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
