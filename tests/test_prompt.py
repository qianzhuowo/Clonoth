"""Prompt 渲染的 YAML 转义回归测试。"""
from __future__ import annotations

from pathlib import Path

import yaml

from engine.node import Node
from engine.prompt import assemble_prompt


def _assemble_yaml_prompt(tmp_path: Path, yaml_text: str) -> str:
    """按 YAML 配置解析 prompt，再通过正式渲染入口返回其内容。"""
    prompt = yaml.safe_load(yaml_text)["prompt"]
    node = Node(id="prompt-test", type="ai", prompt=prompt)
    messages = assemble_prompt(tmp_path, node)
    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


def test_prompt_preserves_yaml_literal_backslashes_and_unicode(tmp_path: Path) -> None:
    yaml_text = r"""
prompt: |-
  Windows: C:\Users\Administrator
  UNC: \\server\share\folder
  ordinary: keep\backslash and literal \n \t \d
  Unicode: 中文与 emoji 😀
"""

    rendered = _assemble_yaml_prompt(tmp_path, yaml_text)

    expected = r"""Windows: C:\Users\Administrator
UNC: \\server\share\folder
ordinary: keep\backslash and literal \n \t \d
Unicode: 中文与 emoji 😀"""
    assert rendered == expected


def test_prompt_keeps_newline_already_decoded_by_yaml(tmp_path: Path) -> None:
    yaml_text = r'prompt: "第一行\n第二行 😀"'

    rendered = _assemble_yaml_prompt(tmp_path, yaml_text)

    assert rendered == "第一行\n第二行 😀"
