from __future__ import annotations

from engine.hooks import hook_registry


def register_builtins() -> None:
    """Register all built-in engine handlers.

    Why: ai_step should install core handlers through the same registry used by
    future plugins. How: register named handlers; HookRegistry replaces handlers
    with the same name, making repeated calls safe. Purpose: keep run_ai_node
    idempotent and avoid duplicated hook side effects.
    """
    from .approval import ApprovalHandler
    from .attachment import AttachmentCollector
    from .compact import CompactChecker
    from .finish_guard import FinishGuardHandler
    from .memory_inject import MemoryInjector
    from .plaintext import PlaintextRetryHandler
    from .preempt import PreemptChecker
    from .skill_inject import SkillInjector
    from .snapshot import ContextSnapshotSaver
    from .usage_tracker import UsageTracker

    hook_registry.register("before_prompt_build", SkillInjector())
    hook_registry.register("before_prompt_build", MemoryInjector())
    hook_registry.register("on_task_end", ContextSnapshotSaver())
    hook_registry.register("on_task_error", ContextSnapshotSaver())
    hook_registry.register("before_step", PreemptChecker())
    hook_registry.register("before_step", CompactChecker())
    hook_registry.register("before_tool_call", FinishGuardHandler())
    hook_registry.register("before_tool_call", ApprovalHandler())
    hook_registry.register("after_tool_call", AttachmentCollector())
    hook_registry.register("after_llm_call", UsageTracker())
    hook_registry.register("before_response", PlaintextRetryHandler())
