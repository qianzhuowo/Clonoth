"""engine/ai_step.py LLM 重试机制单元测试。

运行: python scripts/test_retry.py
"""
from __future__ import annotations

import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# 桩对象
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, ok, error=None, status_code=None, text=None, tool_calls=None, usage=None, thinking=None, raw=None):
        self.ok = ok
        self.error = error
        self.status_code = status_code
        self.text = text
        self.tool_calls = tool_calls or []
        self.thinking = thinking
        self.raw = raw
        self.usage = usage


# ---------------------------------------------------------------------------
# 测试 _is_retryable_error
# ---------------------------------------------------------------------------

from engine.ai_step import _is_retryable_error, _RETRYABLE_STATUS_CODES


def test_retryable_with_retryable_status_codes():
    """429/500/502/503/504 应视为可重试。"""
    for code in (429, 500, 502, 503, 504):
        r = _FakeResp(ok=False, error=f"HTTP {code}", status_code=code)
        assert _is_retryable_error(r), f"status_code={code} 应为可重试"
    print("retryable status codes. OK")


def test_not_retryable_client_errors():
    """400/401/403/404 等客户端错误不应重试。"""
    for code in (400, 401, 403, 404, 422):
        r = _FakeResp(ok=False, error=f"HTTP {code}", status_code=code)
        assert not _is_retryable_error(r), f"status_code={code} 不应重试"
    print("non-retryable client errors. OK")


def test_retryable_network_error():
    """无 status_code（网络异常）应视为可重试。"""
    r = _FakeResp(ok=False, error="Connection timeout", status_code=None)
    assert _is_retryable_error(r)
    print("network error retryable. OK")


def test_ok_response_not_retryable():
    """成功的响应不应进入重试逻辑。"""
    r = _FakeResp(ok=True, text="hello")
    assert not _is_retryable_error(r)
    print("ok response not retryable. OK")


# ---------------------------------------------------------------------------
# 测试重试循环行为（模拟 provider）
# ---------------------------------------------------------------------------

class _CountingProvider:
    """前 N 次调用返回指定错误，之后返回成功。"""

    def __init__(self, fail_count: int, fail_status: int | None = 429):
        self.fail_count = fail_count
        self.fail_status = fail_status
        self.call_count = 0

    async def chat(self, *, messages, tools):
        self.call_count += 1
        if self.call_count <= self.fail_count:
            return _FakeResp(ok=False, error=f"HTTP {self.fail_status or 'timeout'}", status_code=self.fail_status)
        return _FakeResp(ok=True, text="success", usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})

    async def chat_stream(self, *, messages, tools, on_text=None, on_thinking=None):
        return await self.chat(messages=messages, tools=tools)


async def _simulate_retry_loop(provider, max_retries=3, initial_delay=0.01, max_delay=0.1, backoff=2.0):
    """模拟 ai_step.py 中的重试循环核心逻辑。

    不依赖完整的 run_ai_node，而是提取重试循环的关键逻辑进行测试。
    """
    resp = None
    attempt = 0

    while True:
        resp = await provider.chat(messages=[], tools=None)

        if not resp.ok and _is_retryable_error(resp) and attempt < max_retries:
            attempt += 1
            delay = min(initial_delay * (backoff ** (attempt - 1)), max_delay)
            await asyncio.sleep(delay)
            resp = None
            continue

        break

    return resp, attempt, provider.call_count


def test_retry_succeeds_after_failures():
    """前2次429，第3次成功 → 应重试2次后成功。"""
    provider = _CountingProvider(fail_count=2, fail_status=429)
    resp, attempts, calls = asyncio.run(_simulate_retry_loop(provider))
    assert resp.ok, "应最终成功"
    assert attempts == 2, f"应重试2次，实际 {attempts}"
    assert calls == 3, f"应调用3次，实际 {calls}"
    print("retry succeeds after 2 failures. OK")


def test_retry_exhausted():
    """连续4次429，max_retries=3 → 应重试3次后失败。"""
    provider = _CountingProvider(fail_count=10, fail_status=429)
    resp, attempts, calls = asyncio.run(_simulate_retry_loop(provider, max_retries=3))
    assert not resp.ok, "应最终失败"
    assert attempts == 3, f"应重试3次，实际 {attempts}"
    assert calls == 4, f"应调用4次，实际 {calls}"
    print("retry exhausted after 3 retries. OK")


def test_no_retry_on_client_error():
    """401错误 → 不应重试，直接失败。"""
    provider = _CountingProvider(fail_count=10, fail_status=401)
    resp, attempts, calls = asyncio.run(_simulate_retry_loop(provider))
    assert not resp.ok, "应直接失败"
    assert attempts == 0, f"不应重试，实际 {attempts}"
    assert calls == 1, f"应只调用1次，实际 {calls}"
    print("no retry on 401. OK")


def test_no_retry_when_disabled():
    """max_retries=0 → 不重试。"""
    provider = _CountingProvider(fail_count=10, fail_status=429)
    resp, attempts, calls = asyncio.run(_simulate_retry_loop(provider, max_retries=0))
    assert not resp.ok, "应直接失败"
    assert attempts == 0, f"不应重试，实际 {attempts}"
    assert calls == 1, f"应只调用1次，实际 {calls}"
    print("no retry when max_retries=0. OK")


def test_retry_on_network_error():
    """无 status_code 的网络错误 → 应重试。"""
    provider = _CountingProvider(fail_count=1, fail_status=None)
    resp, attempts, calls = asyncio.run(_simulate_retry_loop(provider))
    assert resp.ok, "应最终成功"
    assert attempts == 1, f"应重试1次，实际 {attempts}"
    assert calls == 2, f"应调用2次，实际 {calls}"
    print("retry on network error. OK")


def test_immediate_success_no_retry():
    """首次成功 → 不重试。"""
    provider = _CountingProvider(fail_count=0)
    resp, attempts, calls = asyncio.run(_simulate_retry_loop(provider))
    assert resp.ok
    assert attempts == 0
    assert calls == 1
    print("immediate success. OK")


def test_backoff_delay_calculation():
    """验证指数退避计算：delay = min(initial * backoff^(attempt-1), max_delay)。"""
    initial = 1.0
    backoff = 2.0
    max_d = 10.0

    # attempt 1: min(1.0 * 2^0, 10) = 1.0
    assert min(initial * (backoff ** 0), max_d) == 1.0
    # attempt 2: min(1.0 * 2^1, 10) = 2.0
    assert min(initial * (backoff ** 1), max_d) == 2.0
    # attempt 3: min(1.0 * 2^2, 10) = 4.0
    assert min(initial * (backoff ** 2), max_d) == 4.0
    # attempt 4: min(1.0 * 2^3, 10) = 8.0
    assert min(initial * (backoff ** 3), max_d) == 8.0
    # attempt 5: min(1.0 * 2^4, 10) = 10.0 (capped)
    assert min(initial * (backoff ** 4), max_d) == 10.0
    print("backoff delay calculation. OK")


# ---------------------------------------------------------------------------
# 测试配置读取
# ---------------------------------------------------------------------------

def test_config_defaults():
    """验证 DEFAULT_RUNTIME_CONFIG 包含 retry 配置。"""
    from clonoth_runtime import DEFAULT_RUNTIME_CONFIG, get_int, get_float
    cfg = DEFAULT_RUNTIME_CONFIG

    assert get_int(cfg, "engine.retry.max_retries", -1) == 3
    assert get_float(cfg, "engine.retry.initial_delay_sec", -1) == 1.0
    assert get_float(cfg, "engine.retry.max_delay_sec", -1) == 30.0
    assert get_float(cfg, "engine.retry.backoff_multiplier", -1) == 2.0
    print("config defaults. OK")


def test_retryable_status_set():
    """验证 _RETRYABLE_STATUS_CODES 包含预期值。"""
    expected = {429, 500, 502, 503, 504}
    assert _RETRYABLE_STATUS_CODES == expected, f"预期 {expected}，实际 {_RETRYABLE_STATUS_CODES}"
    print("retryable status set. OK")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_retryable_with_retryable_status_codes()
    test_not_retryable_client_errors()
    test_retryable_network_error()
    test_ok_response_not_retryable()
    test_retry_succeeds_after_failures()
    test_retry_exhausted()
    test_no_retry_on_client_error()
    test_no_retry_when_disabled()
    test_retry_on_network_error()
    test_immediate_success_no_retry()
    test_backoff_delay_calculation()
    test_config_defaults()
    test_retryable_status_set()
    print("\nAll retry tests passed.")
