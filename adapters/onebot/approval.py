"""QQ approval bookkeeping and wording.

Holds pending approvals and the "approval message id -> approval id" map so an
admin can reply to the approval message with "同意/拒绝". Free of nonebot
imports; the adapter sends messages and submits decisions.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any, Dict, Mapping, Optional

from . import config
from .approval_origin import format_approval_origin

_UUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
_ALLOW_WORDS = {"同意", "批准", "通过", "允许", "allow", "approve", "approved", "yes", "y", "ok"}
_DENY_WORDS = {"拒绝", "驳回", "不同意", "deny", "reject", "rejected", "no", "n"}

PENDING_NOTICE = "当前操作需要 Clonoth 管理员审批，已提交等待处理。"
NO_ADMIN_DENY_COMMENT = "QQ adapter denied because CLONOTH_ADMIN_QQ_USERS is not configured."


def normalize_msg_id(message_id: Any) -> str:
    """Normalise OneBot message ids (int / "-123.0" / padded) to one key form."""
    if message_id is None:
        return ""
    key = str(message_id).strip()
    if not key:
        return ""
    try:
        return str(int(float(key))) if "." in key else str(int(key))
    except (ValueError, TypeError):
        return key


def truncate_qq_text(text: str, limit: Optional[int] = None) -> str:
    limit = config.QQ_MESSAGE_LIMIT if limit is None else limit
    if len(text) <= limit:
        return text
    suffix = "\n（内容过长，已截断）"
    return text[: limit - len(suffix)] + suffix


def parse_approval_reply_verb(text: str) -> Optional[str]:
    """Only full decision words count; questions or other text are not decisions."""
    normalized = (text or "").strip().lower()
    if normalized.startswith("审批"):
        normalized = normalized[len("审批"):].strip()
    elif normalized.startswith("approval "):
        normalized = normalized[len("approval "):].strip()
    if normalized in _DENY_WORDS:
        return "deny"
    if normalized in _ALLOW_WORDS:
        return "allow"
    return None


def parse_approval_command(text: str) -> Optional[tuple[str, str]]:
    """Parse "审批 同意 <id>"; id may be a unique prefix of at least 8 chars."""
    parts = (text or "").strip().split()
    if parts and parts[0].lower() in {"审批", "approval"}:
        parts = parts[1:]
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F][0-9a-fA-F-]{7,35}", parts[1]):
        return None
    decision = parse_approval_reply_verb(parts[0])
    return (decision, parts[1].lower()) if decision else None


def approval_summary(
    approval_id: str,
    operation: str,
    details: Mapping[str, Any],
    *,
    origin_line: str = "",
) -> str:
    """Approval request text sent privately to QQ admins."""
    lines = ["【Clonoth 审批请求】"]
    if origin_line:
        lines.append(f"来源：{origin_line}")
    lines.extend([f"ID: {approval_id}", f"操作: {operation or 'unknown'}"])
    for key in ("tool_name", "path", "command", "reason", "safety_level"):
        value = details.get(key)
        if value is not None and value != "":
            lines.append(f"{key}: {str(value)[:1000]}")
    args = details.get("args") or details.get("parameters")
    if args:
        lines.append(f"参数: {str(args)[:1500]}")
    lines.extend([
        "",
        "【快捷审批】直接引用(回复)本消息，发送“同意”或“拒绝”即可。",
        f"或手动发送：审批 同意 {approval_id}",
        f"　　　　　审批 拒绝 {approval_id}",
    ])
    return truncate_qq_text("\n".join(lines))


def decision_result_text(approval_id: str, decision: str, operation: str, ok: bool) -> str:
    if ok:
        verb = "同意" if decision == "allow" else "拒绝"
        return f"已{verb}审批：{approval_id}\n操作：{operation or 'unknown'}"
    return "审批未生效：请求可能已超时、已处理或被拒绝；未确认批准成功。"


class ApprovalBook:
    """Pending approvals plus the bounded message-id map used for reply approval."""

    def __init__(self, max_message_ids: int = 500) -> None:
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.message_ids: "OrderedDict[str, str]" = OrderedDict()
        self.max_message_ids = max(1, int(max_message_ids))

    def add(
        self,
        approval_id: str,
        operation: str,
        details: Mapping[str, Any] | None,
        *,
        conversation_key: str = "",
        session_id: str = "",
        origin: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        info = {
            "operation": operation,
            "details": dict(details or {}),
            "conversation_key": conversation_key,
            "session_id": session_id,
            "origin": dict(origin or {}),
            "created_at": time.time(),
        }
        self.pending[approval_id] = info
        return info

    def remember_message(self, message_id: Any, approval_id: str) -> None:
        key = normalize_msg_id(message_id)
        if not approval_id or not key:
            return
        self.message_ids[key] = approval_id
        self.message_ids.move_to_end(key)
        while len(self.message_ids) > self.max_message_ids:
            self.message_ids.popitem(last=False)

    def resolve_by_reply(self, reply_message_id: Any) -> Optional[str]:
        """Approval id for a replied approval message, only if still pending."""
        key = normalize_msg_id(reply_message_id)
        approval_id = self.message_ids.get(key) if key else None
        return approval_id if approval_id and approval_id in self.pending else None

    def extract_from_text(self, text: str) -> Optional[str]:
        """Pending approval id mentioned in a text (e.g. the quoted summary)."""
        if not text:
            return None
        for approval_id in self.pending:
            if approval_id and approval_id in text:
                return approval_id
        match = _UUID_RE.search(text)
        if match and match.group(1) in self.pending:
            return match.group(1)
        return None

    def resolve_token(self, token: str) -> tuple[Optional[str], str]:
        """Full id or unique prefix -> (approval_id, error)."""
        token = (token or "").strip()
        if not token:
            return None, "缺少审批 ID。"
        if token in self.pending:
            return token, ""
        matches = [aid for aid in self.pending if aid.startswith(token)]
        if len(matches) == 1:
            return matches[0], ""
        if not matches:
            return None, f"没有找到待审批 ID：{token}"
        return None, f"审批 ID 前缀不唯一：{token}"

    def forget(self, approval_id: str) -> Dict[str, Any]:
        """Drop a finished approval and every message id that points to it."""
        info = self.pending.pop(approval_id, {})
        for message_id, mapped in list(self.message_ids.items()):
            if mapped == approval_id:
                self.message_ids.pop(message_id, None)
        return info


def origin_line_for(
    origin: Mapping[str, Any] | None,
    *,
    group_name: Optional[str] = None,
    group_id: Optional[int | str] = None,
    user_name: Optional[str] = None,
    user_id: Optional[int | str] = None,
) -> str:
    """Thin re-export so callers only import from this module."""
    return format_approval_origin(
        origin, group_name=group_name, group_id=group_id, user_name=user_name, user_id=user_id,
    )
