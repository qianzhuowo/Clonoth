"""FinishGuardHandler 的 finish_requires_tool 硬校验单测。

验证：绘图类节点声明 finish_requires_tool 后，模型若未真正成功执行生图工具，
finish 会被拦截并要求真实调用工具，从而杜绝"伪造工具调用"造成的假成功。
"""
import asyncio
from types import SimpleNamespace

from engine.builtin.finish_guard import FinishGuardHandler, _finish_required_tools


def _make_ctx(*, node_extra, succeeded=(), failed=(), pseudo_names=("finish",), real_names=()):
    node = SimpleNamespace(id="draw.novelai_planner", extra=dict(node_extra))
    ls = SimpleNamespace(
        succeeded_real_tools=set(succeeded),
        failed_real_tools=set(failed),
    )
    pseudo_calls = [SimpleNamespace(name=n, arguments={}) for n in pseudo_names]
    real_tool_calls = [{"name": n} for n in real_names]
    return SimpleNamespace(
        node=node,
        step=1,
        tool_calls=[SimpleNamespace(name=n) for n in (*pseudo_names, *real_names)],
        extra={
            "pseudo_calls": pseudo_calls,
            "real_tool_calls": real_tool_calls,
            "loop_state": ls,
        },
    )


def _run(ctx):
    return asyncio.run(FinishGuardHandler().handle(ctx))


def test_required_tools_parsing_variants():
    assert _finish_required_tools(SimpleNamespace(extra={"finish_requires_tool": {"any": ["a", "b"]}})) == {"a", "b"}
    assert _finish_required_tools(SimpleNamespace(extra={"finish_requires_tool": ["x"]})) == {"x"}
    assert _finish_required_tools(SimpleNamespace(extra={"finish_requires_tool": "y"})) == {"y"}
    assert _finish_required_tools(SimpleNamespace(extra={})) == set()
    assert _finish_required_tools(None) == set()


def test_finish_blocked_when_required_tool_never_succeeded():
    ctx = _make_ctx(
        node_extra={"finish_requires_tool": {"any": ["nai_generate_from_plan", "nai_generate"]}},
        succeeded=(),  # 没有任何真实工具成功
    )
    result = _run(ctx)
    assert result is not None
    assert result.block is True
    assert result.reason == "finish_requires_tool"
    assert "nai_generate_from_plan" in result.error_message


def test_finish_blocked_when_required_tool_only_failed():
    ctx = _make_ctx(
        node_extra={"finish_requires_tool": {"any": ["nai_generate_from_plan"]}},
        succeeded=(),
        failed=("nai_generate_from_plan",),  # 调用过但失败（如 429）
    )
    result = _run(ctx)
    assert result is not None and result.block is True
    assert result.reason == "finish_requires_tool"


def test_finish_allowed_when_required_tool_succeeded():
    ctx = _make_ctx(
        node_extra={"finish_requires_tool": {"any": ["nai_generate_from_plan"]}},
        succeeded=("nai_generate_from_plan",),  # 真实成功过
    )
    result = _run(ctx)
    # 未触发硬校验拦截，且本轮 finish 单独出现（无并列工具），返回 None 放行
    assert result is None


def test_node_without_requirement_not_affected():
    ctx = _make_ctx(node_extra={}, succeeded=())
    result = _run(ctx)
    assert result is None


def test_colocation_guard_still_works():
    # finish 与真实工具并列时，原有并列拦截仍然生效
    ctx = _make_ctx(
        node_extra={},
        succeeded=(),
        pseudo_names=("finish",),
        real_names=("nai_generate_from_plan",),
    )
    result = _run(ctx)
    assert result is not None and result.block is True
    assert result.reason == "finish_colocated"


if __name__ == "__main__":
    test_required_tools_parsing_variants()
    test_finish_blocked_when_required_tool_never_succeeded()
    test_finish_blocked_when_required_tool_only_failed()
    test_finish_allowed_when_required_tool_succeeded()
    test_node_without_requirement_not_affected()
    test_colocation_guard_still_works()
    print("all finish_requires_tool tests passed")
