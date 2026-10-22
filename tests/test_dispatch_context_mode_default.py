"""dispatch 默认 context_mode 推导测试。

[AutoC 2026-07-09] 覆盖 2026-07-09 的默认模式改动：
- 显式传 context_mode 时，无论目标是否 persistent 都优先采用传入值；
- 未显式传时，target 节点 persistent=true → accumulate；否则 → fresh。

同时覆盖两处实现入口：
- supervisor 侧 TaskRouterMixin._resolve_dispatch_context_mode / _node_is_persistent；
- engine 侧 pseudo_handlers._target_is_persistent。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

# tests 从源码 checkout 运行，无安装包：把仓库根加入 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402


# --------------------------------------------------------------------------- #
#  fixtures / helpers
# --------------------------------------------------------------------------- #

_PERSISTENT_NODE = """\
id: {nid}
type: ai
name: "persistent test node"
description: "persistent"
persistent: true
prompt: "hi"
"""

_ONESHOT_NODE = """\
id: {nid}
type: ai
name: "oneshot test node"
description: "oneshot"
persistent: false
prompt: "hi"
"""


def _write_node(workspace: Path, nid: str, body: str) -> None:
    nodes_dir = workspace / "config" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    (nodes_dir / f"{nid}.yaml").write_text(body.format(nid=nid), encoding="utf-8")


def _make_state(workspace: Path) -> SupervisorState:
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-ctx-mode")
    return SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )


# --------------------------------------------------------------------------- #
#  supervisor 侧：_resolve_dispatch_context_mode / _node_is_persistent
# --------------------------------------------------------------------------- #

def test_supervisor_persistent_node_defaults_to_accumulate(tmp_path: Path) -> None:
    _write_node(tmp_path, "persist_agent", _PERSISTENT_NODE)
    st = _make_state(tmp_path)

    assert st._node_is_persistent("persist_agent") is True
    # 未显式传 context_mode → persistent 节点默认 accumulate
    assert st._resolve_dispatch_context_mode("persist_agent", None) == "accumulate"
    assert st._resolve_dispatch_context_mode("persist_agent", "") == "accumulate"


def test_supervisor_non_persistent_node_defaults_to_fresh(tmp_path: Path) -> None:
    _write_node(tmp_path, "scout_like", _ONESHOT_NODE)
    st = _make_state(tmp_path)

    assert st._node_is_persistent("scout_like") is False
    # 未显式传 context_mode → 非 persistent 节点默认 fresh
    assert st._resolve_dispatch_context_mode("scout_like", None) == "fresh"
    assert st._resolve_dispatch_context_mode("scout_like", "") == "fresh"


def test_supervisor_missing_node_defaults_to_fresh(tmp_path: Path) -> None:
    st = _make_state(tmp_path)
    # 找不到节点配置时保守视为非 persistent → fresh
    assert st._node_is_persistent("nonexistent") is False
    assert st._resolve_dispatch_context_mode("nonexistent", None) == "fresh"


def test_supervisor_explicit_mode_wins_over_persistent(tmp_path: Path) -> None:
    _write_node(tmp_path, "persist_agent", _PERSISTENT_NODE)
    _write_node(tmp_path, "scout_like", _ONESHOT_NODE)
    st = _make_state(tmp_path)

    # 显式传参优先：persistent 节点也能被显式指定 fresh/fork
    assert st._resolve_dispatch_context_mode("persist_agent", "fresh") == "fresh"
    assert st._resolve_dispatch_context_mode("persist_agent", "fork") == "fork"
    # 非 persistent 节点也能被显式指定 accumulate
    assert st._resolve_dispatch_context_mode("scout_like", "accumulate") == "accumulate"
    # 前后空白会被裁剪
    assert st._resolve_dispatch_context_mode("scout_like", "  fork  ") == "fork"


# --------------------------------------------------------------------------- #
#  engine 侧：pseudo_handlers._target_is_persistent
# --------------------------------------------------------------------------- #

def _fake_ls(workspace: Path):
    return SimpleNamespace(rctx=SimpleNamespace(workspace_root=workspace))


def test_engine_target_is_persistent(tmp_path: Path) -> None:
    from engine.inference.pseudo_handlers import _target_is_persistent

    _write_node(tmp_path, "persist_agent", _PERSISTENT_NODE)
    _write_node(tmp_path, "scout_like", _ONESHOT_NODE)
    ls = _fake_ls(tmp_path)

    assert _target_is_persistent(ls, "persist_agent") is True
    assert _target_is_persistent(ls, "scout_like") is False
    # 缺省/未知节点保守返回 False
    assert _target_is_persistent(ls, "nonexistent") is False
    assert _target_is_persistent(ls, "") is False
