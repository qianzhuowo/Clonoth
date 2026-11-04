"""Leaked thinking in the visible reply is moved to reasoning; normal replies are untouched."""
from __future__ import annotations

from providers.base import ProviderResponse

from engine.thinking_leak import move_leaked_thinking, split_leaked_thinking

GATEWAY = "\u09bf\u09b0thinking\nLet's follow the formatting rules!</body>\u8001\u5927\uff0c\u7ed3\u8bba\u662f\u8fd9\u6837"


def test_gateway_shape_is_split_at_body_marker():
    assert split_leaked_thinking(GATEWAY) == ("Let's follow the formatting rules!", "\u8001\u5927\uff0c\u7ed3\u8bba\u662f\u8fd9\u6837")


def test_old_tag_shape():
    assert split_leaked_thinking("<thinking>\nplan\n</thinking>\n\nanswer") == ("plan", "answer")


def test_html_answers_are_untouched():
    for text in (
        "\u95ed\u5408\u6807\u7b7e\u5199\u5728\u6700\u540e\uff1a</body></html>",
        "```html\n<body>\n</body>\n```",
        "thinking about it, </body> closes the body",
        "Overthinking\n</body> is fine",
        "thinking is good\n</body>",
    ):
        assert split_leaked_thinking(text) is None, text


def test_marker_inside_code_in_thought_is_skipped():
    text = "\u09bf\u09b0thinking\nThe user asks about `</body>` and\n```\n</body>\n```\nok</body>Real answer"
    thought, reply = split_leaked_thinking(text)
    assert reply == "Real answer"
    assert "`</body>`" in thought


def test_unterminated_thought_becomes_reasoning_only():
    assert split_leaked_thinking("<thinking>\nstill planning") == ("still planning", "")


def test_move_into_response_keeps_existing_reasoning():
    resp = ProviderResponse(ok=True, text=GATEWAY, reasoning="native")
    assert move_leaked_thinking(resp) is True
    assert resp.text == "\u8001\u5927\uff0c\u7ed3\u8bba\u662f\u8fd9\u6837"
    assert resp.reasoning == "native\n\nLet's follow the formatting rules!"
    assert resp.provider_meta["_leaked_thinking_moved"] is True

    plain = ProviderResponse(ok=True, text="hello")
    assert move_leaked_thinking(plain) is False and plain.text == "hello"
