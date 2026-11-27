"""Planner preset field: listed to the model, matched by name/alias, applied by the system."""
import json

import pytest

from plugins.draw.planner.contract import ScenePlannerError, parse_submitted_scene_plan
from plugins.draw.planner.tool import create_submit_scene_plan_tool

PRESETS = [{"id": "p1", "name": "默认"}, {"id": "p2", "name": "厚涂", "aliases": ["ciloranko"]}]
IMAGE = {"index": 1, "title": "t", "orientation": "portrait", "scene": "1girl, garden", "characters": []}


def _plan(**params):
    return [{"name": "submit_scene_plan", "arguments": json.dumps({"images": [IMAGE], **params}, ensure_ascii=False)}]


def test_tool_lists_presets_only_when_there_is_a_choice():
    props = create_submit_scene_plan_tool(presets=PRESETS)["function"]["parameters"]["properties"]
    assert props["preset"]["type"] == "string" and "厚涂（别名：ciloranko）" in props["preset"]["description"]
    assert "preset" not in create_submit_scene_plan_tool(presets=PRESETS[:1])["function"]["parameters"]["properties"]
    assert "preset" not in create_submit_scene_plan_tool()["function"]["parameters"]["properties"]


def test_preset_matched_by_alias_and_applied_to_every_task():
    out = parse_submitted_scene_plan(_plan(preset="CILORANKO"), presets=PRESETS)
    assert out["preset"] == "p2" and [t["preset"] for t in out["tasks"]] == ["p2"]
    assert out["tasks"][0]["scene"] == "1girl, garden"  # the scene text is untouched


def test_no_preset_keeps_default_and_unknown_asks_for_correction():
    out = parse_submitted_scene_plan(_plan(preset=""), presets=PRESETS)
    assert "preset" not in out and "preset" not in out["tasks"][0]
    with pytest.raises(ScenePlannerError) as info:
        parse_submitted_scene_plan(_plan(preset="水彩"), presets=PRESETS)
    assert info.value.code == "TOOL_ARGUMENTS_SCHEMA_INVALID" and info.value.details["expected"] == ["默认", "厚涂"]
