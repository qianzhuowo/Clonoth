"""Verify engine/compact.py logic."""
import asyncio
import sys

sys.path.insert(0, ".")

from engine.compact import compact_messages, should_compact
from providers.base import BaseProvider, ProviderResponse


class MockProvider(BaseProvider):
    async def chat(self, *, messages, tools):
        return ProviderResponse(
            ok=True,
            text="Summary: user asked to fix bug in main.py. File edited. Tests passed.",
            usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        )


class FailProvider(BaseProvider):
    async def chat(self, *, messages, tools):
        return ProviderResponse(ok=False, error="timeout")


async def main():
    provider = MockProvider(model="test")

    # ---- should_compact: disabled threshold ----
    assert should_compact([{"role": "user", "content": "abcdef"}], 0) is False
    print("should_compact disabled threshold. OK")

    # ---- should_compact: with last_prompt_tokens (direct) ----
    dummy = [{"role": "user", "content": "hello"}]
    assert should_compact(dummy, 100, last_prompt_tokens=101) is True
    assert should_compact(dummy, 100, last_prompt_tokens=100) is False
    assert should_compact(dummy, 100, last_prompt_tokens=50) is False
    print("should_compact with last_prompt_tokens. OK")

    # ---- should_compact: char estimation fallback (no prompt_tokens) ----
    # 30 chars -> 30 // 3 = 10 estimated tokens
    plain_msgs = [{"role": "user", "content": "a" * 30}]
    assert should_compact(plain_msgs, 10) is False   # 10 > 10 is False
    assert should_compact(plain_msgs, 9) is True     # 10 > 9 is True
    print("should_compact char estimation fallback. OK")

    # ---- should_compact: multimodal text parts (char estimation) ----
    # 2 + 2 = 4 chars -> 4 // 3 = 1 estimated token
    multimodal_msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "ab"},
                {"type": "image_url", "image_url": {"url": "file://demo.png"}},
                {"type": "text", "text": "cd"},
            ],
        }
    ]
    assert should_compact(multimodal_msgs, 1) is False  # 1 > 1 is False
    assert should_compact(multimodal_msgs, 0) is False  # threshold 0 disables
    print("should_compact multimodal (char estimation). OK")

    # Build system + 20 messages
    msgs = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(20):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"Message {i} content here."})

    result = await compact_messages(provider, msgs, keep_recent=4)
    assert len(result) == 6, f"Expected 6, got {len(result)}"
    assert result[0]["role"] == "system"
    assert "summary" in result[1]["content"].lower()
    print(f"compact_messages with system: {len(msgs)} -> {len(result)} messages. OK")

    # Boundary: exactly keep_recent + 2 messages should stay unchanged
    boundary = [{"role": "system", "content": "sys"}]
    for i in range(5):
        boundary.append({"role": "user", "content": f"Boundary {i}"})
    boundary_result = await compact_messages(provider, boundary, keep_recent=4)
    assert boundary_result is boundary
    print("Boundary message count unchanged. OK")

    # No system message
    no_system = []
    for i in range(8):
        role = "user" if i % 2 == 0 else "assistant"
        no_system.append({"role": role, "content": f"No system {i}"})
    no_system_result = await compact_messages(provider, no_system, keep_recent=4)
    assert len(no_system_result) == 5, f"Expected 5, got {len(no_system_result)}"
    assert no_system_result[0]["role"] == "user"
    assert "summary" in str(no_system_result[0]["content"]).lower()
    print("No system message compaction. OK")

    # Too few messages
    short = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    short_result = await compact_messages(provider, short, keep_recent=4)
    assert short_result is short
    print("Short list unchanged. OK")

    # LLM failure
    failed_result = await compact_messages(FailProvider(model="fail"), msgs, keep_recent=4)
    assert len(failed_result) == len(msgs)
    assert failed_result == msgs
    print("LLM failure returns original. OK")

    print("All tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
