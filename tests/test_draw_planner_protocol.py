"""plugins/draw/planner 纯函数层测试：合同校验、工具 schema、布局投影、消息组装。"""
from __future__ import annotations

import json

import pytest

from plugins.draw.planner import contract, frame, layout, tool
from plugins.draw.planner.assembly import TOOL_PROTOCOL_MARKER, build_chain_preview, build_planner_messages, plan_messages
from plugins.draw.planner.handoff import APPROVAL_RESULT_TEXT, SCENE_PLANNER_ACTION
from plugins.draw.planner.messages import PlannerMessagesInvalid, assert_planner_messages
from plugins.draw.planner.profile import normalize_profile, novelai_v45_profile

PRESENT = [
    {"name": "艾丽丝", "aliases": ["小艾", "Alice"], "type": "girl", "danbooru": "alice_(wonderland)",
     "appearance": "blonde hair, blue eyes", "outfits": [{"name": "校服", "tags": "school uniform"}]},
]


def _plan(images):
    return [{"name": "submit_scene_plan", "arguments": json.dumps({"images": images}, ensure_ascii=False)}]


def _image(**overrides):
    base = {"index": 1, "title": "那只手终于探了出来", "orientation": "portrait", "scene": "indoors, window",
            "characters": [{"name": "小艾", "costume": "school uniform", "action": "smile"}]}
    base.update(overrides)
    return base


# ---------- contract ----------

def test_parse_normalizes_alias_and_clears_known_appear():
    result = contract.parse_submitted_scene_plan(
        _plan([_image(characters=[{"name": "Alice", "costume": "", "action": "smile", "appear": "red hair", "center": "a1"}])]),
        present_characters=PRESENT, center_mode="grid")
    char = result["tasks"][0]["chars"][0]
    assert char["name"] == "艾丽丝"
    assert char["appear"] == ""
    assert char["center"] == {"x": 0.1, "y": 0.1}
    assert result["tasks"][0]["orientation"] == "portrait"
    assert "argumentRepair" not in result


def test_unknown_character_requires_appear():
    with pytest.raises(contract.ScenePlannerError) as info:
        contract.parse_submitted_scene_plan(
            _plan([_image(characters=[{"name": "路人", "costume": "", "action": "stand"}])]), present_characters=PRESENT)
    assert info.value.code == "TOOL_ARGUMENTS_SCHEMA_INVALID"
    assert info.value.details["path"] == "images[0].characters[0].appear"
    assert "未知角色必须填写外貌" in info.value.message


def test_exact_and_cap_limits():
    with pytest.raises(contract.ScenePlannerError) as info:
        contract.parse_submitted_scene_plan(_plan([_image()]), present_characters=PRESENT, max_images=2)
    assert info.value.details["rule"] == "本次必须恰好包含 2 项"
    with pytest.raises(contract.ScenePlannerError) as info:
        contract.parse_submitted_scene_plan(_plan([_image(), _image(index=2)]), present_characters=PRESENT, max_plan_images=1)
    assert info.value.details["rule"] == "本次最多包含 1 项"
    with pytest.raises(contract.ScenePlannerError) as info:
        contract.parse_submitted_scene_plan(
            _plan([_image(characters=[{"name": "小艾", "costume": "", "action": "a"}] * 2)]),
            present_characters=PRESENT, max_characters_per_image=1)
    assert info.value.details["rule"] == "最多包含 1 人"


def test_title_truncated_and_orientation_fallback():
    result = contract.parse_submitted_scene_plan(
        _plan([_image(title="一二三四五六七八九十一二三四五六", orientation="square")]), present_characters=PRESENT)
    task = result["tasks"][0]
    assert task["title"] == "一二三四五六七八九十一二三四"
    assert task["orientation"] == "portrait"


def test_tool_call_protocol_errors():
    with pytest.raises(contract.ScenePlannerError) as info:
        contract.parse_submitted_scene_plan([], context="preset / openai / gpt")
    assert info.value.code == "TOOL_CALL_MISSING" and "（preset / openai / gpt）" in info.value.message
    with pytest.raises(contract.ScenePlannerError) as info:
        contract.parse_submitted_scene_plan([{"name": "a", "arguments": "{}"}, {"name": "b", "arguments": "{}"}])
    assert info.value.code == "TOOL_CALL_MULTIPLE"
    with pytest.raises(contract.ScenePlannerError) as info:
        contract.parse_submitted_scene_plan([{"name": "search_web", "arguments": "{}"}])
    assert info.value.code == "TOOL_CALL_NAME_INVALID"
    assert contract.get_error_category(info.value) == contract.ErrorCategory.TOOL_PROTOCOL
    assert contract.is_correction_error(info.value)


def test_no_image_tasks_and_missing_images():
    with pytest.raises(contract.ScenePlannerError) as info:
        contract.parse_submitted_scene_plan(_plan([]))
    assert info.value.code == "NO_IMAGE_TASKS"
    with pytest.raises(contract.ScenePlannerError) as info:
        contract.parse_submitted_scene_plan([{"name": "submit_scene_plan", "arguments": "{}"}])
    assert info.value.details["path"] == "parameters.images"


def test_json_repair_missing_root_closer_and_trailing_closers():
    body = json.dumps({"images": [_image()]}, ensure_ascii=False)
    truncated = body[:-1]  # 去掉根 }
    result = contract.parse_submitted_scene_plan(
        [{"name": "submit_scene_plan", "arguments": truncated}], present_characters=PRESENT)
    assert result["argumentRepair"]["kind"] == "missing_root_closer"
    extra = body + "}]"
    result = contract.parse_submitted_scene_plan(
        [{"name": "submit_scene_plan", "arguments": extra}], present_characters=PRESENT)
    assert result["argumentRepair"]["kind"] == "trailing_closers"
    with pytest.raises(contract.ScenePlannerError) as info:
        contract.parse_submitted_scene_plan(
            [{"name": "submit_scene_plan", "arguments": truncated}], present_characters=PRESENT, allow_repair=False)
    assert info.value.code == "TOOL_ARGUMENTS_INVALID_JSON"
    # 数组内部截断不修复
    assert contract.repair_scene_plan_arguments('{"images": [{"index": 1, "scene": "a') is None


def test_normalized_center_mode():
    result = contract.parse_submitted_scene_plan(
        _plan([_image(characters=[{"name": "小艾", "costume": "", "action": "a", "center": {"x": "0.2", "y": 0.8}}])]),
        present_characters=PRESENT, center_mode="normalized")
    assert result["tasks"][0]["chars"][0]["center"] == {"x": 0.2, "y": 0.8}
    with pytest.raises(contract.ScenePlannerError) as info:
        contract.parse_submitted_scene_plan(
            _plan([_image(characters=[{"name": "小艾", "costume": "", "action": "a", "center": {"x": 2, "y": 0}}])]),
            present_characters=PRESENT, center_mode="normalized")
    assert info.value.details["path"] == "images[0].characters[0].center.x"


def test_correction_result_and_retry_text():
    try:
        contract.parse_submitted_scene_plan([])
    except contract.ScenePlannerError as error:
        payload = contract.create_correction_result(error)
        assert payload["ok"] is False and payload["error"]["code"] == "TOOL_CALL_MISSING"
        assert payload["instruction"].startswith("你没有调用 Tool")
        assert "上一轮没有交出有效的工具调用" in contract.build_tool_protocol_retry_correction(error)
        sig = contract.get_correction_signature(error)
        assert json.loads(sig)[0] == "TOOL_CALL_MISSING"
    schema_error = contract.ScenePlannerError("x", "TOOL_ARGUMENTS_SCHEMA_INVALID", {"path": "p", "rule": "r"})
    assert contract.build_tool_protocol_retry_correction(schema_error) == ""


# ---------- tool ----------

def test_tool_schema_reflects_profile_and_limits():
    t = tool.create_submit_scene_plan_tool(max_images=2, max_characters_per_image=3, profile=novelai_v45_profile())
    params = t["function"]["parameters"]
    images = params["properties"]["images"]
    assert images["minItems"] == 2 and images["maxItems"] == 2
    item = images["items"]
    assert "insert_after" not in item["required"]
    chars = item["properties"]["characters"]
    assert chars["maxItems"] == 3
    props = chars["items"]["properties"]
    assert props["center"]["pattern"] == "^[A-E][1-5]$"
    assert "source#" in props["interact"]["description"]
    assert "post_prompt" not in item["properties"]
    plain = tool.create_submit_scene_plan_tool(profile={"center_mode": "none", "post_prompt": "append"})
    plain_props = plain["function"]["parameters"]["properties"]["images"]["items"]["properties"]
    assert "center" not in plain_props["characters"]["items"]["properties"]
    assert "post_prompt" in plain_props


def test_tool_description_overrides_and_listing():
    t = tool.create_submit_scene_plan_tool(descriptions={
        "tool": "自定义工具说明", "images[].scene": "自定义 scene", "images[].characters[].name": "  ", "nope": "x"})
    assert t["function"]["description"] == "自定义工具说明"
    item = t["function"]["parameters"]["properties"]["images"]["items"]["properties"]
    assert item["scene"]["description"] == "自定义 scene"
    assert item["characters"]["items"]["properties"]["name"]["description"] == tool.NAME_DESCRIPTION
    listed = {f["path"]: f["description"] for f in tool.list_field_descriptions(t)}
    assert listed["tool"] == "自定义工具说明"
    assert "images[].characters[].costume" in listed
    defaults = tool.default_field_descriptions(novelai_v45_profile())
    assert defaults["images[].characters[].center"] == tool.CENTER_GRID_DESCRIPTION


# ---------- layout ----------

def test_layout_default_and_sparse_storage():
    assert layout.layout_for_storage(None) is None
    assert layout.layout_for_storage(layout.create_default_layout()) is None
    custom = {"version": 2, "blocks": [{"id": b} for b in layout.BUILTIN_BLOCK_IDS] + [
        {"id": "msg-a", "role": "user", "name": "附加", "content": "hi", "enabled": True, "afterApproval": True}]}
    stored = layout.layout_for_storage(custom)
    assert stored is not None and stored["blocks"][-1]["afterApproval"] is True


def test_layout_normalize_repairs_order_and_ids():
    raw = {"version": 1, "blocks": [
        {"id": "x1", "role": "system", "content": "lead"},
        {"id": "guide"},
        {"id": "opening"},
        {"id": "approval", "role": "user", "content": "bad-id"},
        {"id": "content"},
        {"id": "m2", "role": "bot", "content": "bad-role"},
        {"id": "x1", "role": "user", "content": "dup"},
    ]}
    normalized = layout.normalize_layout(raw)
    ids = [b["id"] for b in normalized.layout["blocks"]]
    assert normalized.repaired
    assert ids[0] == "x1"  # leading
    builtin_ids = [i for i in ids if layout.is_builtin_id(i)]
    assert builtin_ids == list(layout.BUILTIN_BLOCK_IDS)
    customs = [b for b in normalized.layout["blocks"] if not layout.is_builtin_id(b["id"])]
    assert len(customs) == 4
    assert all(b["id"] != "approval" for b in customs)
    bad_role = next(b for b in customs if b["content"] == "bad-role")
    assert bad_role["role"] == "user" and bad_role["enabled"] is False
    stages = layout.project_layout(normalized.layout)
    assert [s.id for s in stages] == ["setup", "materials", "guide"]
    assert [b["content"] for b in stages[0].leading_blocks] == ["lead"]
    # "approval"→重生成 id 的块出现在 setup 之后（最近经过阶段为 guide 之前的 opening→setup）
    assert any(b["content"] == "bad-id" for b in stages[0].custom_blocks)
    assert any(b["content"] in ("bad-role", "dup") for b in stages[1].custom_blocks)


# ---------- frame ----------

def test_character_info_and_content_block():
    info = frame.build_character_info(PRESENT)
    assert info.startswith("【已录入角色】\n（name 照抄")
    assert "- name: 艾丽丝 [girl] | danbooru: alice_(wonderland)" in info
    assert "  别名: 小艾, Alice" in info
    assert "服装参考: 校服（school uniform）" in info
    assert frame.build_character_info([]) == "【已录入角色】: 无"
    block = frame.build_content_block(None, character_info=info, draw_request="画她 {{drawRequest}}")
    assert block.count("画她 {{drawRequest}}") == 1 and "近期对话" not in block
    assert frame.missing_content_placeholders("{{characterInfo}}") == ["{{drawRequest}}"]
    assert frame.build_limits_line(0, 2, 4) == "本次提交数量约束：images 最多包含 4 项；每项 characters 最多 2 人。"
    assert frame.build_limits_line(1, 0, 4) == "本次提交数量约束：images 必须恰好包含 1 项。"
    assert frame.build_limits_line(0, 0, 0) == ""
    assert "NovelAI Diffusion V4.5" in frame.build_frame_text(novelai_v45_profile())
    assert normalize_profile({"centerMode": "normalized"}).center_mode == "normalized"


# ---------- assembly ----------

def _texts():
    return {"opening": "开场", "frame": "材料说明", "content": "<content>正文</content>",
            "guide": "## 指南", "rules": "## 规则", "limits": "约束行"}


def test_assembly_native_order_and_marker_removed():
    lay = {"version": 2, "blocks": [
        {"id": "lead", "role": "system", "content": "前置", "enabled": True},
        *[{"id": b} for b in ("opening", "frame", "tools")],
        {"id": "after-setup", "role": "user", "content": "setup 后", "enabled": True},
        {"id": "off", "role": "user", "content": "停用", "enabled": False},
        {"id": "content"},
        {"id": "guide"}, {"id": "rules"}, {"id": "limits"},
        {"id": "tail", "role": "user", "content": "最后提醒", "enabled": True, "afterApproval": True},
    ]}
    messages = build_planner_messages(lay, _texts())
    assert_planner_messages(messages)
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "user", "assistant", "tool", "assistant", "tool", "assistant", "tool", "user"]
    assert messages[0]["content"] == "前置\n\n开场\n\n材料说明"
    assert TOOL_PROTOCOL_MARKER not in json.dumps(messages, ensure_ascii=False)
    assert messages[1]["content"] == "setup 后"
    assert messages[2]["content"] == SCENE_PLANNER_ACTION
    assert messages[3]["tool_calls"][0]["function"]["name"] == "read_scene_materials"
    assert json.loads(messages[4]["content"]) == {"content": "<content>正文</content>"}
    assert json.loads(messages[6]["content"]) == {"content": "## 指南\n\n## 规则\n\n约束行"}
    assert json.loads(messages[8]["content"]) == {"approved": True, "content": APPROVAL_RESULT_TEXT}
    assert messages[9]["content"] == "最后提醒"
    assert "停用" not in json.dumps(messages, ensure_ascii=False)


def test_assembly_text_protocol_injects_tool_text_and_marker_stripped_from_custom():
    lay = {"version": 2, "blocks": [{"id": b} for b in layout.BUILTIN_BLOCK_IDS] + [
        {"id": "c", "role": "user", "content": f"a{TOOL_PROTOCOL_MARKER}b", "enabled": True}]}
    messages = build_planner_messages(lay, _texts(), tool_protocol_text="## 工具协议\n...")
    assert messages[0]["content"].endswith("## 工具协议\n...")
    # 未标 afterApproval 的自定义块排在 guide 阶段之后、提交确认之前
    custom = next(m for m in messages if m["role"] == "user" and m["content"] != SCENE_PLANNER_ACTION)
    assert custom["content"] == "ab"
    assert messages.index(custom) == len(messages) - 3
    assert json.loads(messages[-1]["content"])["approved"] is True


def test_empty_guide_and_rules_omitted():
    texts = _texts()
    texts["guide"] = ""
    texts["rules"] = "   "
    messages = build_planner_messages(None, texts)
    guide_result = next(m for m in messages if m.get("tool_call_id") == "draw_read_drawing_guide")
    assert json.loads(guide_result["content"]) == {"content": "约束行"}


def test_chain_preview_matches_send_order():
    t = tool.create_submit_scene_plan_tool()
    chain = build_chain_preview(None, _texts(), tool=t, field_descriptions=tool.list_field_descriptions(t))
    assert chain[0]["sections"][0]["key"] == "topSystem"
    assert chain[0]["sections"][2]["key"] == "toolDeclaration"
    assert chain[-1]["role"] == "tools" and chain[-1]["fields"][0]["path"] == "tool"
    templates = plan_messages(None, _texts())
    assert len(chain) == len(templates) + 1
    # 固定消息：content 是原文；工具返回另带 rendered（实际发出去的 JSON 包装），供面板预览
    approval = next(m for m in chain if m["role"] == "tool" and m["stageId"] == "approval")["sections"][0]
    assert approval["fixed"] is True and approval["content"] == APPROVAL_RESULT_TEXT
    assert json.loads(approval["rendered"]) == {"approved": True, "content": APPROVAL_RESULT_TEXT}
    call = next(m for m in chain if m["role"] == "assistant" and m["stageId"] == "approval")["sections"][0]
    assert call["fixed"] is True and "rendered" not in call
    action = next(m for m in chain if m["role"] == "user")["sections"][0]
    assert action["fixed"] is True and action["content"] == SCENE_PLANNER_ACTION


def test_assert_messages_rejects_broken_chain():
    with pytest.raises(PlannerMessagesInvalid):
        assert_planner_messages([{"role": "assistant", "content": "x", "tool_calls": [{"id": "c1"}]}])
    with pytest.raises(PlannerMessagesInvalid):
        assert_planner_messages([{"role": "system", "content": "s"}])
    with pytest.raises(PlannerMessagesInvalid):
        assert_planner_messages([{"role": "user", "content": "   "}])
