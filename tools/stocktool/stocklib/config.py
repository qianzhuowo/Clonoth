from __future__ import annotations

QUOTE_TTL_SEC = {
    "cn": 45,
    "a": 45,
    "etf": 45,
    "index": 45,
    "hk": 60,
    "us": 60,
}

RESOLVER_TTL_SEC = 24 * 60 * 60
FAILURE_TTL_SEC = 20
DEFAULT_MAX_ITEMS = 10
HARD_MAX_ITEMS = 30
DISCLAIMER = "数据可能存在延迟，仅供信息参考和技术研究，不构成投资建议。市场有风险，投资需谨慎。"
