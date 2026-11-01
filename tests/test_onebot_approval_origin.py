import importlib.util
from pathlib import Path

# Load the module file directly: importing the adapters.onebot package would
# pull in nonebot, which is not required for this pure formatter.
_PATH = Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "approval_origin.py"
_spec = importlib.util.spec_from_file_location("onebot_approval_origin", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_group_with_name_and_requester() -> None:
    origin = {"conversation_key": "qq_group:abc", "target_type": "group", "user_id": "u1"}
    assert mod.format_approval_origin(origin, group_name="测试群", user_name="小明") == "【群】测试群（发起人：小明）"


def test_group_falls_back_to_id_then_key() -> None:
    origin = {"conversation_key": "qq_group:abc"}
    assert mod.format_approval_origin(origin, group_id=42) == "【群】群 42"
    assert mod.format_approval_origin(origin) == "【群】qq_group:abc"


def test_private_prefers_name() -> None:
    origin = {"conversation_key": "qq_private:p1", "user_id": "u2"}
    assert mod.format_approval_origin(origin, user_name="阿花") == "【私聊】阿花"
    assert mod.format_approval_origin(origin) == "【私聊】u2"


def test_unknown_window() -> None:
    assert mod.format_approval_origin({}) == "【未知窗口】"
    assert mod.format_approval_origin({"conversation_key": "discord:1"}) == "【其他】discord:1"


def test_names_are_single_line_and_truncated() -> None:
    out = mod.format_approval_origin({"target_type": "private"}, user_name="a\nb" + "x" * 100)
    assert "\n" not in out
    assert len(out) <= len("【私聊】") + 40
