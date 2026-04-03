"""验证 v3 统一协议下的 AI 节点执行逻辑。

用法：python scripts/verify_engine.py

不需要真实 LLM 或 Supervisor，全部用 mock。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

os.environ.setdefault("OPENAI_API_KEY", "mock-key")
os.environ.setdefault("OPENAI_MODEL", "mock-model")

from providers.base import ProviderResponse, ToolCall
from providers.openai import OpenAIProvider

from engine.node import load_node
from engine.prompt import assemble_prompt
from engine.protocol import ACTION_DISPATCH, ACTION_FINISH, ACTION_ASK


class MockProvider:
    """OpenAIProvider 的 monkey-patch mock。

    * 有 delegate_targets 的节点 → dispatch_node
    * 无 delegate_targets 的节点 → finish
    """

    def __init__(self) -> None:
        self._orig_chat = None
        self._orig_chat_stream = None

    def install(self) -> None:
        self._orig_chat = OpenAIProvider.chat
        self._orig_chat_stream = OpenAIProvider.chat_stream
        OpenAIProvider.chat = self._mock_chat  # type: ignore[assignment]
        OpenAIProvider.chat_stream = self._mock_chat_stream  # type: ignore[assignment]

    def uninstall(self) -> None:
        if self._orig_chat:
            OpenAIProvider.chat = self._orig_chat  # type: ignore[assignment]
        if self._orig_chat_stream:
            OpenAIProvider.chat_stream = self._orig_chat_stream  # type: ignore[assignment]

    @staticmethod
    async def _mock_chat(
        self_provider: OpenAIProvider,
        messages: list,
        tools: list | None = None,
        *,
        temperature: float | None = None,
        tool_choice: str | None = None,
    ) -> ProviderResponse:
        has_finish = False
        has_ask = False
        has_dispatch = False
        for t in (tools or []):
            fn = t.get("function", {}).get("name", "")
            if fn == "finish":
                has_finish = True
            elif fn == "ask":
                has_ask = True
            elif fn == "dispatch_node":
                has_dispatch = True

        # 有 dispatch → 测试 dispatch
        # 无 dispatch → 测试 finish
        if has_dispatch:
            return ProviderResponse(
                ok=True,
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call_mock_dispatch",
                        name="dispatch_node",
                        arguments={"target": "bootstrap.executor", "instruction": "测试委派"},
                    )
                ],
            )
        if has_finish:
            return ProviderResponse(
                ok=True,
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call_mock_finish",
                        name="finish",
                        arguments={"text": "执行完成", "summary": "done"},
                    )
                ],
            )
        return ProviderResponse(ok=True, text="mock fallback", tool_calls=[])

    @staticmethod
    async def _mock_chat_stream(*args, **kwargs):
        raise NotImplementedError("streaming not mocked")


# ─── Tests ───

def test_node_loading() -> None:
    print("[test] node loading...")
    for nid in ["bootstrap.shell_orchestrator", "bootstrap.executor", "bootstrap.cmd_reviewer"]:
        n = load_node(WORKSPACE, nid)
        assert n is not None
        assert n.type == "ai"
        assert n.prompt
        assert len(n.prompt) > 20
        print(f"  {nid}: model={n.model or '(default)'}")
    orch = load_node(WORKSPACE, "bootstrap.shell_orchestrator")
    assert orch is not None


def test_delegate_targets() -> None:
    print("[test] delegate_targets (from node YAML)...")
    orch = load_node(WORKSPACE, "bootstrap.shell_orchestrator")
    assert orch is not None
    assert "bootstrap.executor" in orch.delegate_targets
    print(f"  orchestrator delegates: {orch.delegate_targets}")

    cmd = load_node(WORKSPACE, "bootstrap.cmd_reviewer")
    assert cmd is not None
    assert cmd.delegate_targets == []
    print(f"  cmd_reviewer delegates: {cmd.delegate_targets} (empty, correct)")


def test_prompt_assembly() -> None:
    print("[test] prompt assembly...")
    node = load_node(WORKSPACE, "bootstrap.executor")
    assert node is not None
    prompt = assemble_prompt(WORKSPACE, node)
    assert len(prompt) > 50
    assert "{{include:" not in prompt
    # 确认包含新伪工具名
    assert "finish" in prompt
    assert "dispatch_node" in prompt
    print(f"  executor prompt: {len(prompt)} chars")


async def test_ai_node_execution() -> None:
    print("[test] AI node execution (mock)...")
    import httpx

    from engine.ai_step import run_ai_node
    from engine.context import RunContext
    from toolbox.registry import ToolRegistry

    mock = MockProvider()
    mock.install()
    try:
        node = load_node(WORKSPACE, "bootstrap.cmd_reviewer")
        assert node is not None
        registry = ToolRegistry(workspace_root=WORKSPACE, tools_dir=WORKSPACE / "tools")

        # cmd_reviewer 无 delegate_targets → mock 返回 finish
        async with httpx.AsyncClient(trust_env=False) as http:
            rctx = RunContext(
                workspace_root=WORKSPACE,
                supervisor_url="http://127.0.0.1:9999",
                session_id="test-session",
                worker_id="test-worker",
                http=http,
                llm_http=http,
                api_key="mock-key",
                default_model="mock-model",
                task_id="task-cmd",
            )
            provider = OpenAIProvider(http=http, api_key="mock-key", base_url="https://example.invalid", model="mock")
            action = await run_ai_node(
                rctx=rctx,
                provider=provider,
                registry=registry,
                node=node,
                instruction="执行 ls",
                history=[],
            )
            assert action.action == ACTION_FINISH, f"expected finish, got {action.action}"
            result_text = action.result.get("text", "")
            assert result_text, "finish result should have text"
            print(f"  cmd_reviewer action={action.action}, text={result_text[:80]}")

        # orchestrator 有 delegate_targets → mock 返回 dispatch
        node2 = load_node(WORKSPACE, "bootstrap.shell_orchestrator")
        assert node2 is not None
        async with httpx.AsyncClient(trust_env=False) as http:
            rctx2 = RunContext(
                workspace_root=WORKSPACE,
                supervisor_url="http://127.0.0.1:9999",
                session_id="test-session",
                worker_id="test-worker",
                http=http,
                llm_http=http,
                api_key="mock-key",
                default_model="mock-model",
                task_id="task-orchestrator",
            )
            provider2 = OpenAIProvider(http=http, api_key="mock-key", base_url="https://example.invalid", model="mock")
            action2 = await run_ai_node(
                rctx=rctx2,
                provider=provider2,
                registry=registry,
                node=node2,
                instruction="你好",
                history=[],
            )
            assert action2.action == ACTION_DISPATCH, f"expected dispatch, got {action2.action}"
            assert action2.target_node, "dispatch should have target_node"
            print(f"  orchestrator action={action2.action}, target={action2.target_node}")
    finally:
        mock.uninstall()


def main() -> None:
    print("=" * 60)
    print("Clonoth Engine Verification (v3 Protocol — finish/ask/dispatch)")
    print("=" * 60)
    test_node_loading()
    test_delegate_targets()
    test_prompt_assembly()
    asyncio.run(test_ai_node_execution())
    print()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
