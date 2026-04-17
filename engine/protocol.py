from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
#  v3 统一动作协议
# ---------------------------------------------------------------------------

ACTION_DISPATCH = "dispatch"
ACTION_FINISH = "finish"
ACTION_FAIL = "fail"
ACTION_CANCELLED = "cancelled"
ACTION_PREEMPTED = "preempted"


@dataclass
class TaskAction:
    """节点执行后返回的统一动作。

    所有节点（AI 或 Tool）执行完毕后，只能返回以下四种动作之一：
      - dispatch:   委派给另一个节点（AI 或 Tool）
      - finish:     任务完成或需要补充信息，把结果交回去（Supervisor 根据 caller 决定交给谁）
      - fail:       执行失败
      - cancelled:  被取消
      - preempted:  被软打断（上下文已保存）
    """

    action: str  # dispatch | finish | fail | cancelled | preempted
    node_id: str = ""  # 产出此动作的节点 id

    # dispatch 时填写
    target_node: str = ""  # 委派目标节点 id
    dispatch_input: dict[str, Any] = field(default_factory=dict)
    dispatch_batch: list[dict[str, Any]] = field(default_factory=list)  # 批量委派 [{kind, target, instruction/arguments, ...}]

    # finish 时填写
    result: dict[str, Any] = field(default_factory=dict)

    # fail 时填写
    error: str = ""

    # 通用
    context_ref: str = ""  # 当前节点的上下文快照引用
    # Child Session 隔离（Phase B）：子节点使用的 child session ID。
    # 非空时表示此 task 的消息存储在 child session 的 JSONL 中，而非 snapshot。
    child_session_id: str = ""
    summary: str = ""  # 简短摘要（用于事件日志、进度展示）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "action": self.action,
            "node_id": self.node_id,
            "context_ref": self.context_ref,
            "summary": self.summary,
        }
        # Child Session 隔离（Phase B）：将 child_session_id 写入 task result
        if self.child_session_id:
            d["child_session_id"] = self.child_session_id
        if self.action == ACTION_DISPATCH:
            d["target_node"] = self.target_node
            d["dispatch_input"] = dict(self.dispatch_input)
            if self.dispatch_batch:
                d["dispatch_batch"] = list(self.dispatch_batch)
        elif self.action == ACTION_FINISH:
            d["result"] = dict(self.result)
        elif self.action == ACTION_FAIL:
            d["error"] = self.error
        return d
