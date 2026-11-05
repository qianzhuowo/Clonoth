"""A gateway error page returned as reply text becomes a failed call."""
from __future__ import annotations

from providers.base import ProviderResponse

from engine.builtin.fallback_provider import _is_retryable as fallback_retryable
from engine.gateway_error_page import mark_gateway_error_page
from engine.inference.llm_call import _is_retryable_error

PAGE = (
    "### **Proxy error (HTTP 400 Bad Request)**\n\nThe proxy encountered an error while trying to send your prompt "
    "to the API. Further details are provided below.\n\n----\n\n*Unrecognized error from the API. (Requests ending "
    "with a model turn are not supported.)*\n\nRequest ID: 690013e8\n\n```\n{\n  \"error\": {\n    \"message\": "
    "\"Requests ending with a model turn are not supported.\",\n    \"type\": \"INVALID_ARGUMENT\"\n  }\n}\n```\n\n\n\n"
    "<!-- oai-proxy-error -->"
)


def test_client_error_page_skips_same_model_retry_but_falls_back():
    resp = ProviderResponse(ok=True, text=PAGE)
    assert mark_gateway_error_page(resp) is True
    assert (resp.ok, resp.text, resp.status_code) == (False, "", 200)
    assert resp.error == "gateway error page (HTTP 400): Requests ending with a model turn are not supported."
    assert _is_retryable_error(resp) is False
    assert fallback_retryable(resp.status_code, resp.error) is True


def test_transient_error_page_is_retried():
    resp = ProviderResponse(ok=True, text="### **Proxy error (HTTP 503 Service Unavailable)**\n\nbusy")
    assert mark_gateway_error_page(resp) is True
    assert resp.status_code == 503 and _is_retryable_error(resp) is True


def test_normal_replies_are_untouched():
    for text in ("\u521a\u624d\u8fde\u7206\u4e24\u4e2a HTTP 400 Proxy error\uff0c\u662f\u4e0a\u4e0b\u6587\u592a\u957f",
                 "Proxy error (HTTP 400) \u662f\u4ec0\u4e48\u610f\u601d\uff1f", "hello"):
        resp = ProviderResponse(ok=True, text=text)
        assert mark_gateway_error_page(resp) is False and resp.ok and resp.text == text
    with_tools = ProviderResponse(ok=True, text=PAGE, tool_calls=[{"id": "1"}])
    assert mark_gateway_error_page(with_tools) is False
