"""Which primary failures move on to the fallback chain."""
from engine.builtin.fallback_provider import _is_retryable


def test_channel_specific_rejections_fall_back():
    for code in (401, 402, 404, 429, 500, 503):
        assert _is_retryable(code, "All relay-c/gemini-x error: Not Found")
    assert _is_retryable(None)


def test_malformed_request_and_auth_403_stay():
    assert not _is_retryable(400, "bad request")
    assert not _is_retryable(403, "forbidden")
    assert _is_retryable(403, "blocked by safety filter")
