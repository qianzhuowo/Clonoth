from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
#  v3 统一动作协议
# ---------------------------------------------------------------------------

ACTION_DISPATCH = "dispatch"
ACTION_FINISH = "finish"
ACTION_ASK = "ask"
ACTION_FAIL = "fail"
ACTION_CANCELLED = "cancelled"


@dataclass
class TaskAction:
    """节点执行后返回的统一动作。

    所有节点（AI 或 Tool）执行完毕后，只能返回以下五种动作之一：
      - dispatch:   委派给另一个节点（AI 或 Tool）
      - finish:     任务完成，把结果交回去（Supervisor 根据 caller 决定交给谁）
      - ask:        信息不足，向调用方提问（Supervisor 路由同 finish）
      - fail:       执行失败
      - cancelled:  被取消
    """

    action: str  # dispatch | finish | ask | fail | cancelled
    node_id: str = ""  # 产出此动作的节点 id

    # dispatch 时填写
    target_node: str = ""  # 委派目标节点 id
    dispatch_input: dict[str, Any] = field(default_factory=dict)
    dispatch_batch: list[dict[str, Any]] = field(default_factory=list)  # 批量委派 [{kind, target, instruction/arguments, ...}]

    # finish / ask 时填写
    result: dict[str, Any] = field(default_factory=dict)

    # fail 时填写
    error: str = ""

    # 通用
    context_ref: str = ""  # 当前节点的上下文快照引用
    summary: str = ""  # 简短摘要（用于事件日志、进度展示）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "action": self.action,
            "node_id": self.node_id,
            "context_ref": self.context_ref,
            "summary": self.summary,
        }
        if self.action == ACTION_DISPATCH:
            d["target_node"] = self.target_node
            d["dispatch_input"] = dict(self.dispatch_input)
            if self.dispatch_batch:
                d["dispatch_batch"] = list(self.dispatch_batch)
        elif self.action in (ACTION_FINISH, ACTION_ASK):
            d["result"] = dict(self.result)
        elif self.action == ACTION_FAIL:
            d["error"] = self.error
        return d
