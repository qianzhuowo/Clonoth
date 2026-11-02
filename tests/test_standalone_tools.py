"""Standalone external tools migrated from the old project: discovery and offline behavior."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from toolbox.registry import _extract_tool_spec

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
STANDALONE = ("read_web", "web_search", "exa_search", "x_search", "sci_calc", "remote_exec")


def _run(name: str, args: dict, env: dict | None = None) -> tuple[int, dict]:
    env = dict(env if env is not None else os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # Windows pipes default to the locale codec
    proc = subprocess.run(
        [sys.executable, str(TOOLS / f"{name}.py")],
        input=json.dumps(args), capture_output=True, text=True, encoding="utf-8", timeout=60,
        env=env, cwd=str(ROOT),
    )
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("name", STANDALONE)
def test_tool_spec_is_discoverable(name):
    spec, _ = _extract_tool_spec(TOOLS / f"{name}.py")
    assert spec and spec.get("name") == name
    assert isinstance(spec.get("input_schema"), dict)


def test_remote_exec_carries_no_real_host():
    text = (TOOLS / "remote_exec.py").read_text(encoding="utf-8")
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)


def test_remote_exec_requires_configured_host():
    env = {k: v for k, v in os.environ.items() if k != "REMOTE_EXEC_HOST"}
    rc, payload = _run("remote_exec", {"command": "echo hi"}, env)
    assert rc == 1 and payload["ok"] is False
    assert "REMOTE_EXEC_HOST" in payload["error"]


def test_remote_exec_requires_command():
    rc, payload = _run("remote_exec", {}, {**os.environ, "REMOTE_EXEC_HOST": "example-alias"})
    assert rc == 1 and "command is required" in payload["error"]


def test_sci_calc_evaluates_expression():
    pytest.importorskip("sympy")
    spec, _ = _extract_tool_spec(TOOLS / "sci_calc.py")
    props = spec["input_schema"].get("properties", {})
    key = "expression" if "expression" in props else next(iter(props))
    rc, payload = _run("sci_calc", {key: "2+3*4"})
    assert rc == 0, payload
    assert "14" in json.dumps(payload, ensure_ascii=False)


def test_stocktool_pack_exposes_exactly_two_tools():
    names = sorted(
        (spec or {}).get("name")
        for spec, _ in (_extract_tool_spec(p) for p in (TOOLS / "stocktool").rglob("*.py") if "__pycache__" not in p.parts)
        if spec
    )
    assert names == ["stocktool_history", "stocktool_quote"]


IMAGE_TOOLS = {
    "gemini_image.py": "gemini_image",
    "gpt_image_2.py": "gpt_image_2",
    "drawtools/draw_context.py": "draw_context",
    "drawtools/nai_cleanup.py": "nai_cleanup",
    "drawtools/nai_generate.py": "nai_generate",
    "drawtools/nai_generate_from_plan.py": "nai_generate_from_plan",
}


@pytest.mark.parametrize("rel, name", sorted(IMAGE_TOOLS.items()))
def test_image_tool_spec_is_discoverable(rel, name):
    spec, _ = _extract_tool_spec(TOOLS / rel)
    assert spec and spec.get("name") == name


@pytest.mark.parametrize("rel", ["gemini_image.py", "gpt_image_2.py"])
def test_image_tool_has_no_system_models_dependency(rel):
    assert "system_models" not in (TOOLS / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel, var", [("gemini_image.py", "CLONOTH_IMAGE_GEMINI_API_KEY"), ("gpt_image_2.py", "CLONOTH_IMAGE_GPT_API_KEY")])
def test_image_tool_without_key_fails_naming_its_variable(tmp_path, rel, var):
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "BASE_URL" not in k and not k.startswith("CLONOTH_IMAGE_")}
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(TOOLS / rel)], input=json.dumps({"prompt": "a cat"}),
        capture_output=True, text=True, encoding="utf-8", timeout=60, env=env, cwd=str(tmp_path),
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert proc.returncode == 1 and payload["ok"] is False
    assert var in payload["error"]
