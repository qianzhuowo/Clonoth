"""面板请求结构编辑器（plugins/draw/web/layout-editor.js）的布局纯函数与 planner/layout.py 的一致性。

前端草稿由 JS 规范化，落盘由 Python 规范化；两边对同一输入必须给出同一布局。
需要本机有 node；没有时整个文件跳过。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plugins.draw.planner import layout

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 才能运行面板脚本")

EDITOR_JS = Path(__file__).resolve().parents[1] / "plugins" / "draw" / "web" / "layout-editor.js"

RUNNER = r"""
const fs = require('fs');
const vm = require('vm');
const window = {};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), { window });
const E = window.DrawLayoutEditor;
const c = E.contract;
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const ids = l => l.blocks.map(b => b.id);
const mk = (id, role, name) => ({ id, role, name, content: 'text of ' + id, enabled: true });

const out = { constants: {
  version: c.LAYOUT_VERSION, approval: c.APPROVAL_MARKER_ID, roles: c.CUSTOM_BLOCK_ROLES,
  stages: c.STAGES.map(s => ({ id: s.id, label: s.label, blockIds: s.blockIds })),
  builtins: c.BUILTIN_BLOCKS.map(b => ({ id: b.id, label: b.label, role: b.role, required: !!b.required })),
} };
out.normalized = input.cases.map(raw => c.normalizeLayout(raw).layout);
out.projected = input.cases.map(raw => c.projectLayout(raw).map(s => ({
  id: s.id, leading: s.leadingBlocks.map(b => b.id), custom: s.customBlocks.map(b => b.id), trailing: s.trailingBlocks.map(b => b.id),
})));

const L0 = c.createDefaultLayout();
const L1 = c.addBlockAfter(L0, mk('a', 'system', 'A'), 'tools');
const L2 = c.addBlockAfter(L1, mk('b', 'user', 'B'));
const L3 = c.addBlockAfter(L2, mk('c', 'assistant', 'C'), 'opening');
const dup = c.duplicateBlocks(L3, ['a', 'opening'], () => 0.5);
const rem = c.removeBlocks(L3, ['a', 'frame']);
const created = c.createCustomBlock({ role: 'nope', name: '  n  ' }, ids(L3), () => 0);
out.ops = {
  L1, L2, L3,
  afterTrailing: c.addBlockAfter(L3, mk('d', 'user', 'D'), 'b'),
  moveDown: c.moveBlocks(L3, ['a'], 1),
  moveStageUp: c.moveBlocks(L3, ['content'], -1),
  moveStagePastStage: c.moveBlocks(L3, ['limits'], -1),
  moveAtEdge: c.moveBlocks(L3, ['b'], 1),
  moveGroup: c.moveBlocks(L3, ['c', 'a'], 1),
  moveIntoTrailing: c.moveBlocks(c.moveBlocks(c.moveBlocks(L3, ['a'], 1), ['a'], 1), ['a'], 1),
  reorderBeforeApproval: c.reorderBlocks(L3, ['opening', 'frame', 'tools', 'c', 'a', 'content', 'guide', 'rules', 'limits', 'b', 'approval']),
  reorderPartial: c.reorderBlocks(L3, ['a', 'c']),
  disabled: c.setBlocksEnabled(L3, ['a', 'opening'], false),
  updated: c.updateBlock(L3, 'a', { role: 'user', name: 'A2', content: 'new', enabled: false }),
  updateBuiltin: c.updateBlock(L3, 'opening', { role: 'user', content: 'x' }),
  removedOne: c.removeBlock(L3, 'c'),
  removeBuiltin: c.removeBlock(L3, 'limits'),
  dupLayout: dup.layout, removedLayout: rem.layout,
};
out.meta = {
  dupCopies: dup.copies, dupSkipped: dup.skipped, removedIds: rem.removedIds, removeSkipped: rem.skipped, created,
  inputUntouched: JSON.stringify(L3) === JSON.stringify(c.addBlockAfter(L2, mk('c', 'assistant', 'C'), 'opening')),
  defaultIsDefault: c.isDefaultLayout(L0), customIsDefault: c.isDefaultLayout(L1),
};
const S = E.sortable;
out.sortable = {
  insert: S.computeInsertIndex(25, [{ top: 0, bottom: 10 }, { top: 10, bottom: 20 }, { top: 20, bottom: 30 }], 0),
  mark: S.dropMarkFor(3, 0, 2), markFirst: S.dropMarkFor(3, 2, 0),
  move: S.moveIdToIndex(['x', 'y', 'z'], 'x', 2),
  groupInsert: S.computeGroupInsertIndex(35, [{ top: 0, bottom: 10 }, { top: 10, bottom: 20 }, { top: 20, bottom: 30 }, { top: 30, bottom: 40 }], [0, 1]),
  groupMove: S.moveGroupToIndex(['x', 'y', 'z', 'w'], ['x', 'z'], 2),
  moved: E.findMovedId(['x', 'y', 'z'], ['y', 'z', 'x']),
  previewText: E.previewText('\n\n  第一行  \n第二行'),
};
process.stdout.write(JSON.stringify(out));
"""

CASES = [
    None,
    {"version": 2, "blocks": []},
    {"version": 2, "blocks": "oops"},
    "garbage",
    42,
    [{"id": "x1", "role": "user", "name": " 名称 ", "content": "c"}],
    {"version": 1, "blocks": [
        {"id": "lead", "role": "system", "name": "", "content": "before everything", "enabled": True},
        {"id": "opening"},
        {"id": "inside", "role": "user", "name": "in stage", "content": "x"},
        {"id": "frame"}, {"id": "tools"}, {"id": "content"},
        {"id": "mid", "role": "assistant", "name": "mid", "content": "y", "enabled": False},
        {"id": "early-trailing", "role": "user", "name": "t", "content": "z", "afterApproval": True},
        {"id": "guide"}, {"id": "rules"}, {"id": "limits"},
        {"id": "tail", "role": "user", "name": "tail", "content": "w"},
    ]},
    {"version": 2, "blocks": [
        {"id": "limits"}, {"id": "opening"}, {"id": "opening"},
        {"role": "user", "content": "no id"},
        {"id": "msg-000001", "role": "user", "content": "collides with the fallback id"},
        {"id": "dup", "role": "user", "content": "1"},
        {"id": "dup", "role": "system", "content": "2"},
        {"id": "approval", "role": "user", "content": "reserved id"},
        {"id": "bad id!", "role": "user", "content": "illegal id"},
        {"id": 7, "role": "user", "content": "non-string id"},
        {"id": "role-x", "role": "tool", "content": "invalid role", "enabled": True},
        {"id": "num", "role": "user", "name": 5, "content": None},
        {"id": "obj", "role": "user", "name": {"a": 1}, "content": ["x"]},
        {"id": "en-str", "role": "user", "content": "e", "enabled": "yes"},
        {"id": "en-zero", "role": "user", "content": "e", "enabled": 0},
        {"id": "en-null", "role": "user", "content": "e", "enabled": None},
        {"id": "aa-str", "role": "user", "content": "e", "afterApproval": "true"},
        "not an object", None, 3,
    ]},
]


@pytest.fixture(scope="module")
def js(tmp_path_factory):
    runner = tmp_path_factory.mktemp("layout_editor_js") / "runner.js"
    runner.write_text(RUNNER, encoding="utf-8")
    proc = subprocess.run([NODE, str(runner), str(EDITOR_JS)], input=json.dumps({"cases": CASES}).encode("utf-8"),
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return json.loads(proc.stdout.decode("utf-8"))


def _ids(value):
    return [b["id"] for b in value["blocks"]]


def test_constants_match_backend(js):
    c = js["constants"]
    assert c["version"] == layout.LAYOUT_VERSION
    assert c["approval"] == layout.APPROVAL_MARKER_ID
    assert tuple(c["roles"]) == layout.CUSTOM_BLOCK_ROLES
    assert [(s["id"], s["label"], tuple(s["blockIds"])) for s in c["stages"]] == [
        (s.id, s.label, s.block_ids) for s in layout.STAGES]
    assert [(b["id"], b["label"], b["role"], b["required"]) for b in c["builtins"]] == [
        (b.id, b.label, b.role, b.required) for b in layout.BUILTIN_BLOCKS]


@pytest.mark.parametrize("index", range(len(CASES)))
def test_normalize_matches_backend(js, index):
    assert js["normalized"][index] == layout.normalize_layout(CASES[index]).layout


@pytest.mark.parametrize("index", range(len(CASES)))
def test_projection_matches_backend(js, index):
    expected = [{"id": s.id, "leading": [b["id"] for b in s.leading_blocks], "custom": [b["id"] for b in s.custom_blocks],
                 "trailing": [b["id"] for b in s.trailing_blocks]} for s in layout.project_layout(CASES[index])]
    assert js["projected"][index] == expected


def test_every_operation_yields_a_layout_the_backend_keeps_as_is(js):
    for name, result in js["ops"].items():
        normalized = layout.normalize_layout(result)
        assert normalized.layout == result, name
        assert not normalized.repaired, (name, normalized.notes)


def test_add_places_new_message_under_anchor(js):
    ops = js["ops"]
    assert _ids(ops["L1"]) == ["opening", "frame", "tools", "a", "content", "guide", "rules", "limits"]
    assert _ids(ops["L2"])[-1] == "b" and ops["L2"]["blocks"][-1]["afterApproval"] is True
    # 锚点在阶段内部：阶段不可拆，新消息落到该阶段之后、已有自定义消息之前
    assert _ids(ops["L3"]) == ["opening", "frame", "tools", "c", "a", "content", "guide", "rules", "limits", "b"]
    after = ops["afterTrailing"]
    assert _ids(after)[-2:] == ["b", "d"] and after["blocks"][-1]["afterApproval"] is True
    assert js["meta"]["inputUntouched"] is True


def test_move_respects_stages_and_approval(js):
    ops = js["ops"]
    moved = ["opening", "frame", "tools", "c", "content", "a", "guide", "rules", "limits", "b"]
    assert _ids(ops["moveDown"]) == moved
    assert _ids(ops["moveStageUp"]) == moved  # 把“知识材料”整组上移一格 = 越过紧邻的自定义消息
    assert ops["moveStagePastStage"] == ops["L3"]  # 阶段之间的先后固定
    assert ops["moveAtEdge"] == ops["L3"]
    assert _ids(ops["moveGroup"]) == ["opening", "frame", "tools", "content", "c", "a", "guide", "rules", "limits", "b"]
    trailing = ops["moveIntoTrailing"]
    assert _ids(trailing) == ["opening", "frame", "tools", "c", "content", "guide", "rules", "limits", "a", "b"]
    assert [b.get("afterApproval") for b in trailing["blocks"][-2:]] == [True, True]


def test_reorder_only_rearranges(js):
    ops = js["ops"]
    before = ops["reorderBeforeApproval"]
    assert _ids(before) == _ids(ops["L3"])
    assert "afterApproval" not in before["blocks"][-1]
    assert _ids(ops["reorderPartial"]) == ["a", "c", "opening", "frame", "tools", "content", "guide", "rules", "limits", "b"]
    assert sorted(_ids(ops["reorderPartial"])) == sorted(_ids(ops["L3"]))


def test_edit_operations_never_touch_builtins(js):
    ops, meta = js["ops"], js["meta"]
    by_id = {b["id"]: b for b in ops["disabled"]["blocks"]}
    assert by_id["a"]["enabled"] is False and by_id["opening"] == {"id": "opening"}
    updated = next(b for b in ops["updated"]["blocks"] if b["id"] == "a")
    assert (updated["role"], updated["name"], updated["content"], updated["enabled"]) == ("user", "A2", "new", False)
    assert ops["updateBuiltin"] == ops["L3"]
    assert "c" not in _ids(ops["removedOne"])
    assert ops["removeBuiltin"] == ops["L3"]
    assert meta["removedIds"] == ["a"] and meta["removeSkipped"] == ["frame"]
    assert "a" not in _ids(ops["removedLayout"]) and "frame" in _ids(ops["removedLayout"])
    assert meta["dupSkipped"] == ["opening"] and [c["sourceId"] for c in meta["dupCopies"]] == ["a"]
    copy_id = meta["dupCopies"][0]["id"]
    dup_ids = _ids(ops["dupLayout"])
    assert dup_ids.index(copy_id) == dup_ids.index("a") + 1
    assert meta["created"] == {"id": "msg-000000", "role": "user", "name": "n", "content": "", "enabled": True}
    assert meta["defaultIsDefault"] is True and meta["customIsDefault"] is False


def test_sortable_pure_helpers(js):
    s = js["sortable"]
    assert s["insert"] == 2
    assert s["mark"] == {"index": 2, "side": "after"}
    assert s["markFirst"] == {"index": 0, "side": "before"}
    assert s["move"] == ["y", "z", "x"]
    assert s["groupInsert"] == 2
    assert s["groupMove"] == ["y", "w", "x", "z"]
    assert s["moved"] == "x"
    assert s["previewText"] == "第一行"
