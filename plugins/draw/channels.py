"""生图渠道是否启用的唯一判定。

某渠道“可用”当且仅当显式配置了该渠道的 model：环境变量或工作区根 ``.env``
中 ``CLONOTH_IMAGE_<X>_MODEL`` 非空。不因工具内部回退主渠道（OPENAI_* 等）就算
可用，避免“假可用”。读取顺序与 tools/gpt_image_2.py、tools/gemini_image.py 一致：
先环境变量，再 .env 文件（Engine 子进程会剥离 ``*_API_KEY``，所以 .env 必须读）。

被渠道门控（gating）和 draw.image_gen 可用性注入（image_gen_inject）共用。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# 渠道 -> (model 环境变量, 生图工具名)
IMAGE_CHANNELS: dict[str, tuple[str, str]] = {
    "gpt": ("CLONOTH_IMAGE_GPT_MODEL", "gpt_image_2"),
    "gemini": ("CLONOTH_IMAGE_GEMINI_MODEL", "gemini_image"),
}
IMAGE_GEN_TOOLS = frozenset(tool for (_env, tool) in IMAGE_CHANNELS.values())
# 两个渠道都未配置时从委派目标中移除的节点
IMAGE_GEN_NODE_ID = "draw.image_gen"


def _read_dotenv(workspace_root: Path) -> dict[str, str]:
    env_path = workspace_root / ".env"
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    except Exception as exc:  # noqa: BLE001
        logger.warning("draw: 读取 .env 失败: %s", exc)
    return values


def image_channel_availability(workspace_root: Path | str) -> dict[str, bool]:
    """返回 {'gpt': bool, 'gemini': bool}。"""
    dotenv = _read_dotenv(Path(workspace_root))
    result: dict[str, bool] = {}
    for channel, (env_key, _tool) in IMAGE_CHANNELS.items():
        value = (os.environ.get(env_key, "") or dotenv.get(env_key, "")).strip()
        result[channel] = bool(value)
    return result
