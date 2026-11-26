/* 请求结构编辑器。
 *
 * 移植自 LittleWhiteBox（Apache-2.0，Copyright 2025 biex）modules/draw/shared/ 下的三个文件：
 *   scene-planner-layout.js  → 第一部分：布局契约（纯函数）
 *   sortable-list.js         → 第二部分：指针拖拽 + 键盘排序
 *   prompt-layout-editor.js  → 第三部分：编辑器界面
 *
 * 与原版的差异：
 *   - ES module 改为无依赖 IIFE，挂 window.DrawLayoutEditor；
 *   - 去掉“附带工具”（沙盒代码）、世界书块、网格排序、多列表 scope、宿主停用说明；
 *   - 阶段与内置块以 plugins/draw/planner/layout.py 为准（“知识材料”阶段只有 content 一块）；
 *   - 固定消息原文与内置块预览不在前端写死，取自后端 POST /preview 的组装结果；
 *   - 内置块的就地编辑表单由本文件创建（原版由宿主页面提供），读写经 options.prompts。
 *
 * 用法：window.DrawLayoutEditor.mount(container, { api, say, confirm?, prompts?, onChange?, collapseId? })
 *   api(path, body)            面板的请求函数；只用来调 '/preview'
 *   say(text, kind)            面板的提示条；只用来报错
 *   prompts.get(key)           → { content, default, is_default } | undefined（同步，已保存的值）
 *   prompts.save(key, content) → Promise；prompts.reset(key) → Promise
 * 返回 { setLayout, getLayout, isDirty, hasUnsavedText, getDraftTexts, refreshBuiltinTexts, refreshPreview, destroy }
 */
(() => {
'use strict';

/* ════════════════════════════ 一、布局契约 ════════════════════════════ */

const LAYOUT_VERSION = 2;
// “提交确认”不是布局里的块，编辑器用这个 id 当它的位置标记；排在它后面的自定义消息存成 afterApproval: true。
const APPROVAL_MARKER_ID = 'approval';

const BUILTIN_BLOCKS = Object.freeze([
  Object.freeze({ id: 'opening', label: '开场与视角', role: 'system' }),
  Object.freeze({ id: 'frame', label: '材料说明与输出去向', role: 'system' }),
  Object.freeze({ id: 'tools', label: '可用工具', role: 'system' }),
  Object.freeze({ id: 'content', label: '角色与绘图请求', role: 'tool', required: true }),
  Object.freeze({ id: 'guide', label: 'TAG指南', role: 'tool' }),
  Object.freeze({ id: 'rules', label: '场景规则', role: 'tool' }),
  Object.freeze({ id: 'limits', label: '数量约束', role: 'tool', required: true }),
]);
const STAGES = Object.freeze([
  Object.freeze({ id: 'setup', label: '角色与工具', blockIds: Object.freeze(['opening', 'frame', 'tools']) }),
  Object.freeze({ id: 'materials', label: '知识材料', blockIds: Object.freeze(['content']) }),
  Object.freeze({ id: 'guide', label: '绘图规范', blockIds: Object.freeze(['guide', 'rules', 'limits']) }),
]);
const BUILTIN_BLOCK_IDS = Object.freeze(STAGES.flatMap(stage => stage.blockIds));
const CUSTOM_BLOCK_ROLES = Object.freeze(['system', 'user', 'assistant']);
const PREBUILT_MESSAGE_LABELS = Object.freeze({ call: '预构建调用', result: '预构建返回' });

const STAGE_BY_BLOCK = new Map(STAGES.flatMap((stage, index) => stage.blockIds.map(id => [id, index])));
const BUILTIN_BY_ID = new Map(BUILTIN_BLOCKS.map(block => [block.id, block]));
const CUSTOM_LABEL_FALLBACK = '自定义消息';
const CUSTOM_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
const CUSTOM_ID_PREFIX = 'msg-';
const CUSTOM_ID_DIGITS = 6;
const CUSTOM_ID_SPACE = 36 ** CUSTOM_ID_DIGITS;
const RANDOM_ATTEMPTS = 32; // random 连续撞库时的上限；超过后改走确定性扫描

const lastOf = list => list[list.length - 1];
const isBuiltinId = id => typeof id === 'string' && BUILTIN_BY_ID.has(id);
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const formatCustomId = n => CUSTOM_ID_PREFIX + n.toString(36).padStart(CUSTOM_ID_DIGITS, '0');

function toIdSet(existingIds) {
  if (existingIds instanceof Set) return new Set(existingIds);
  return new Set(Array.isArray(existingIds) ? existingIds : []);
}

// 在 used 里找一个未占用的 id：先用 random，撞满次数后顺序扫描（确定性，必然终止）。
function pickFreeId(used, random) {
  if (typeof random === 'function') {
    for (let i = 0; i < RANDOM_ATTEMPTS; i += 1) {
      const r = Number(random());
      const n = Number.isFinite(r) ? Math.min(CUSTOM_ID_SPACE - 1, Math.max(0, Math.floor(r * CUSTOM_ID_SPACE))) : 0;
      const id = formatCustomId(n);
      if (!used.has(id) && !isBuiltinId(id)) return id;
    }
  }
  for (let n = 1; ; n += 1) {
    const id = formatCustomId(n);
    if (!used.has(id) && !isBuiltinId(id)) return id;
  }
}

// 规范化时的兜底 id：十进制计数器，与 layout.py 的 _next_free_id 一致（同一输入两边得到同一结果）。
function nextFallbackId(used, counter) {
  for (;;) {
    counter.n += 1;
    const id = CUSTOM_ID_PREFIX + String(counter.n).padStart(CUSTOM_ID_DIGITS, '0');
    if (!used.has(id)) { used.add(id); return id; }
  }
}

// enabled 不是布尔值时按 Python 的真值规则折算（与 layout.py 的 bool(enabled) 一致）。
function coerceEnabled(value) {
  if (value === undefined) return true;
  if (typeof value === 'boolean') return value;
  if (value === null) return false;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') return value.length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(value).length > 0;
  return Boolean(value);
}

// 非字符串字段的宽松转字符串：对象 / 数组宁可当空串。
function coerceText(value) {
  if (typeof value === 'string') return { text: value, coerced: false };
  if (value === undefined || value === null) return { text: '', coerced: false };
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') return { text: String(value), coerced: true };
  return { text: '', coerced: true };
}

// 内置块归位；自定义消息按最近经过的阶段安置（最前 / 各阶段之后 / 提交确认之后）。
function fixStageOrder(blocks) {
  const leading = [];
  const trailing = [];
  const customs = STAGES.map(() => []);
  let stageIndex = -1;
  let lastCustomStage = -1;
  for (const block of blocks) {
    if (STAGE_BY_BLOCK.has(block.id)) stageIndex = STAGE_BY_BLOCK.get(block.id);
    else if (block.afterApproval) trailing.push(block);
    else {
      lastCustomStage = Math.max(lastCustomStage, stageIndex);
      (lastCustomStage < 0 ? leading : customs[lastCustomStage]).push(block);
    }
  }
  return [...leading, ...STAGES.flatMap((stage, index) => [...stage.blockIds.map(id => ({ id })), ...customs[index]]), ...trailing];
}

function createDefaultLayout() {
  return { version: LAYOUT_VERSION, blocks: BUILTIN_BLOCK_IDS.map(id => ({ id })) };
}

function createCustomBlock({ role = 'user', name = '', content = '', enabled = true, afterApproval = false } = {}, existingIds = [], random = Math.random) {
  return {
    id: pickFreeId(toIdSet(existingIds), random),
    role: CUSTOM_BLOCK_ROLES.includes(role) ? role : 'user',
    name: coerceText(name).text.trim(),
    content: coerceText(content).text,
    enabled: enabled !== false,
    ...(afterApproval === true ? { afterApproval: true } : {}),
  };
}

// trimName=false 仅供编辑类纯操作使用：用户边打字边调用时不能把刚输入的空格吃掉。
function normalizeCore(raw, { trimName = true } = {}) {
  if (raw === undefined || raw === null) return { layout: createDefaultLayout(), repaired: false, notes: [] };
  const notes = [];
  let rawBlocks = [];
  if (Array.isArray(raw)) {
    notes.push('根节点是数组，已按块列表处理');
    rawBlocks = raw;
  } else if (typeof raw === 'object') {
    if (raw.version !== undefined) {
      if (typeof raw.version === 'number' && raw.version > LAYOUT_VERSION) notes.push(`布局版本 ${raw.version} 高于当前支持的 ${LAYOUT_VERSION}，已尽力按已知字段读取`);
      else if (raw.version !== 1 && raw.version !== LAYOUT_VERSION) notes.push('布局版本无效，已按当前版本处理');
    }
    if (Array.isArray(raw.blocks)) rawBlocks = raw.blocks;
    else if (raw.blocks !== undefined) notes.push('blocks 不是数组，已忽略');
  } else {
    return { layout: createDefaultLayout(), repaired: true, notes: ['布局格式无效，已恢复默认布局'] };
  }

  // 第一遍：分拣内置 / 自定义，内置只留首次出现；id 缺失 / 非法 / 重复时按出现顺序用计数器兜底。
  // 顺序与取号方式都和 layout.py 的 normalize_layout 一致，前端草稿和后端落盘的结果才对得上。
  const used = new Set([...BUILTIN_BLOCK_IDS, APPROVAL_MARKER_ID]);
  const counter = { n: 0 };
  const seenBuiltin = new Set();
  const slots = [];
  let droppedEntries = 0;
  let duplicateBuiltins = 0;
  let regeneratedIds = 0;
  for (const entry of rawBlocks) {
    if (!isRecord(entry)) { droppedEntries += 1; continue; }
    const id = entry.id;
    if (isBuiltinId(id)) {
      if (seenBuiltin.has(id)) duplicateBuiltins += 1;
      else { seenBuiltin.add(id); slots.push({ builtin: id }); }
      continue;
    }
    const idOk = typeof id === 'string' && CUSTOM_ID_PATTERN.test(id) && !used.has(id);
    if (idOk) used.add(id);
    else regeneratedIds += 1;
    slots.push({ entry, id: idOk ? id : nextFallbackId(used, counter) });
  }

  // 第二遍：生成自定义块。
  const blocks = [];
  let invalidRoles = 0;
  let coercedFields = 0;
  for (const slot of slots) {
    if (slot.builtin) { blocks.push({ id: slot.builtin }); continue; }
    const { entry } = slot;
    const id = slot.id;
    const roleValid = CUSTOM_BLOCK_ROLES.includes(entry.role);
    if (!roleValid) invalidRoles += 1;
    const nameInfo = coerceText(entry.name);
    const contentInfo = coerceText(entry.content);
    const nameText = trimName ? nameInfo.text.trim() : nameInfo.text;
    if (nameInfo.coerced || contentInfo.coerced || (trimName && nameText !== nameInfo.text)) coercedFields += 1;
    if (entry.enabled !== undefined && typeof entry.enabled !== 'boolean') coercedFields += 1;
    blocks.push({
      id,
      role: roleValid ? entry.role : 'user',
      name: nameText,
      content: contentInfo.text,
      // 角色无效时强制停用：宁可不发，也不悄悄发一条用户没选过角色的消息。
      enabled: roleValid && coerceEnabled(entry.enabled),
      ...(entry.afterApproval === true ? { afterApproval: true } : {}),
    });
  }

  if (droppedEntries > 0) notes.push(`丢弃了 ${droppedEntries} 个无效的块条目`);
  if (duplicateBuiltins > 0) notes.push(`丢弃了 ${duplicateBuiltins} 个重复的内置块`);
  if (regeneratedIds > 0) notes.push(`为 ${regeneratedIds} 个自定义块重新生成了 id（缺失、非法或重复）`);
  if (invalidRoles > 0) notes.push(`${invalidRoles} 个自定义块的角色无效，已设为 user 并停用`);
  if (coercedFields > 0) notes.push(`${coercedFields} 处自定义块字段类型不符，已转换`);
  const restored = BUILTIN_BLOCK_IDS.filter(id => !seenBuiltin.has(id)).map(id => BUILTIN_BY_ID.get(id).label);
  if (restored.length > 0) notes.push(`补回了缺失的内置块：${restored.join('、')}`);

  return { layout: { version: LAYOUT_VERSION, blocks: fixStageOrder(blocks) }, repaired: notes.length > 0, notes };
}

function normalizeLayout(raw) {
  try { return normalizeCore(raw); } catch (error) {
    return { layout: createDefaultLayout(), repaired: true, notes: ['布局读取失败，已恢复默认布局'] };
  }
}

function safeNormalize(raw, options) {
  try { return normalizeCore(raw, options).layout; } catch (error) { return createDefaultLayout(); }
}

const layoutFingerprint = layout => JSON.stringify(normalizeLayout(layout).layout);
const DEFAULT_FINGERPRINT = JSON.stringify(createDefaultLayout());
const isDefaultLayout = layout => layoutFingerprint(layout) === DEFAULT_FINGERPRINT;

/** 编辑器和组装器消费同一阶段投影：第一阶段之前的在 leadingBlocks，提交确认之后的在最后一个阶段的 trailingBlocks。 */
function projectLayout(raw) {
  const { blocks } = normalizeLayout(raw).layout;
  const firstTrailing = blocks.findIndex(block => block.afterApproval);
  const stagesEnd = firstTrailing === -1 ? blocks.length : firstTrailing;
  return STAGES.map((stage, index) => {
    const start = blocks.findIndex(block => block.id === stage.blockIds[0]);
    const last = index + 1 === STAGES.length;
    const end = last ? stagesEnd : blocks.findIndex(block => block.id === STAGES[index + 1].blockIds[0]);
    return {
      ...stage,
      leadingBlocks: index === 0 ? blocks.slice(0, start) : [],
      trailingBlocks: last ? blocks.slice(stagesEnd) : [],
      blocks: blocks.slice(start, end),
      customBlocks: blocks.slice(start + stage.blockIds.length, end),
    };
  });
}

function describeBlock(block) {
  const id = isRecord(block) && typeof block.id === 'string' ? block.id : '';
  const builtin = BUILTIN_BY_ID.get(id);
  if (builtin) return { id, builtin: true, role: builtin.role, label: builtin.label };
  const role = isRecord(block) && CUSTOM_BLOCK_ROLES.includes(block.role) ? block.role : 'user';
  const name = isRecord(block) ? coerceText(block.name).text.trim() : '';
  return { id, builtin: false, role, label: name || CUSTOM_LABEL_FALLBACK };
}

// ---- 纯操作：入参先规范化再改副本，结果再规范化一次，保证出来的永远是合法布局，且不改动入参 ----

function editBlocks(layout, edit) {
  const blocks = safeNormalize(layout, { trimName: false }).blocks;
  const next = edit(blocks) || blocks;
  return safeNormalize({ version: LAYOUT_VERSION, blocks: next }, { trimName: false });
}

/** 插到 afterId 那一块的正下方；afterId 不存在（或没给）时放到最后。 */
function addBlockAfter(layout, block, afterId) {
  return editBlocks(layout, blocks => {
    if (!isRecord(block) || isBuiltinId(block.id)) return blocks;
    const at = blocks.findIndex(item => item.id === afterId);
    const next = blocks.slice();
    // 跟在“提交确认之后”的消息后面、或没有锚点放到末尾时，新消息也在提交确认之后。
    const trailing = at === -1 || blocks[at].afterApproval === true;
    next.splice(at === -1 ? next.length : at + 1, 0, trailing ? { ...block, afterApproval: true } : { ...block });
    return next;
  });
}

const toIdList = ids => (Array.isArray(ids) ? ids.filter(id => typeof id === 'string') : []);

/** 复制自定义块：每个副本紧跟在原块后面；内置块和不存在的 id 放进 skipped。 */
function duplicateBlocks(layout, ids, random = Math.random) {
  const wanted = new Set(toIdList(ids));
  const copies = [];
  const normalized = safeNormalize(layout, { trimName: false }).blocks;
  const customIds = new Set(normalized.filter(block => !isBuiltinId(block.id)).map(block => block.id));
  const skipped = Array.from(wanted).filter(id => !customIds.has(id));
  const used = new Set(normalized.map(block => block.id));
  const next = editBlocks(layout, blocks => blocks.flatMap((block) => {
    if (!wanted.has(block.id) || isBuiltinId(block.id)) return [block];
    const id = pickFreeId(used, random);
    used.add(id);
    copies.push({ sourceId: block.id, id });
    return [block, { ...block, id }];
  }));
  return { layout: next, copies, skipped };
}

/** 批量删除自定义块；内置块永远不删，放进 skipped。 */
function removeBlocks(layout, ids) {
  const wanted = new Set(toIdList(ids));
  const existing = safeNormalize(layout, { trimName: false }).blocks;
  const removedIds = existing.filter(block => wanted.has(block.id) && !isBuiltinId(block.id)).map(block => block.id);
  const skipped = Array.from(wanted).filter(id => !removedIds.includes(id));
  const removed = new Set(removedIds);
  return { layout: editBlocks(layout, blocks => blocks.filter(block => !removed.has(block.id))), removedIds, skipped };
}

function setBlocksEnabled(layout, ids, enabled) {
  const wanted = new Set(toIdList(ids));
  return editBlocks(layout, blocks => blocks.map(block => (
    wanted.has(block.id) && !isBuiltinId(block.id) ? { ...block, enabled: enabled !== false } : block
  )));
}

// 移动 / 重排时临时把“提交确认”标记放进块列表：自定义消息越过它，就是在“提交确认之前 / 之后”之间切换；
// 结束后去掉标记，把排在它后面的自定义消息标成 afterApproval。
function withApprovalMarker(blocks, edit) {
  const first = blocks.findIndex(block => block.afterApproval);
  const marked = blocks.slice();
  marked.splice(first === -1 ? marked.length : first, 0, { id: APPROVAL_MARKER_ID });
  const edited = edit(marked);
  const at = edited.findIndex(block => block.id === APPROVAL_MARKER_ID);
  if (at === -1) return edited;
  return edited.filter(block => block.id !== APPROVAL_MARKER_ID).map((block, index) => {
    if (isBuiltinId(block.id)) return block;
    const next = { ...block };
    delete next.afterApproval;
    return index >= at ? { ...next, afterApproval: true } : next;
  });
}

/**
 * 把自定义消息（或传入阶段最后一块的 id 时，整个内置阶段）整体上移 / 下移；完整的内置阶段是不可拆分的移动单位。
 * 上移 / 下移是相对“组外的块”算的：组里不连续时会先并拢到同一处。已在边界时原样返回。
 */
function moveBlocks(layout, ids, direction) {
  return editBlocks(layout, original => withApprovalMarker(original, (blocks) => {
    // 编辑器用阶段的最后一块当“整组”的把手 id：移动它，就是把整个固定阶段挪过相邻的自定义消息。
    const wanted = new Set(toIdList(ids).flatMap((id) => {
      const stage = STAGES.find(item => lastOf(item.blockIds) === id);
      return stage ? [stage.blockIds[0]] : isBuiltinId(id) ? [] : [id];
    }));
    const units = [];
    for (const block of blocks) {
      if (!isBuiltinId(block.id)) units.push([block]);
      else {
        const stage = STAGES.find(item => item.blockIds[0] === block.id);
        if (stage) units.push(stage.blockIds.map(id => ({ id })));
      }
    }
    const group = units.filter(unit => wanted.has(unit[0].id)).flat();
    const step = direction > 0 ? 1 : direction < 0 ? -1 : 0;
    if (!group.length || step === 0) return blocks;
    const rest = units.filter(unit => !wanted.has(unit[0].id));
    const outsideBefore = index => units.slice(0, index).filter(unit => !wanted.has(unit[0].id)).length;
    const first = units.findIndex(unit => wanted.has(unit[0].id));
    const last = units.length - 1 - units.slice().reverse().findIndex(unit => wanted.has(unit[0].id));
    const at = step < 0 ? Math.max(0, outsideBefore(first) - 1) : Math.min(rest.length, outsideBefore(last) + 1);
    return [...rest.slice(0, at).flat(), ...group, ...rest.slice(at).flat()];
  }));
}

function removeBlock(layout, id) {
  return editBlocks(layout, blocks => (isBuiltinId(id) ? blocks : blocks.filter(block => block.id !== id)));
}

function updateBlock(layout, id, patch) {
  return editBlocks(layout, (blocks) => {
    if (isBuiltinId(id) || !isRecord(patch)) return blocks;
    return blocks.map((block) => {
      if (block.id !== id) return block;
      const next = { ...block };
      if (CUSTOM_BLOCK_ROLES.includes(patch.role)) next.role = patch.role;
      if (typeof patch.name === 'string') next.name = patch.name;
      if (typeof patch.content === 'string') next.content = patch.content;
      if (typeof patch.enabled === 'boolean') next.enabled = patch.enabled;
      return next;
    });
  });
}

function reorderBlocks(layout, orderedIds) {
  return editBlocks(layout, original => withApprovalMarker(original, (blocks) => {
    if (!Array.isArray(orderedIds)) return blocks;
    const byId = new Map(blocks.map(block => [block.id, block]));
    const placed = new Set();
    const head = [];
    for (const id of orderedIds) {
      if (!byId.has(id) || placed.has(id)) continue;
      placed.add(id);
      head.push(byId.get(id));
    }
    // 没被列出的块保持原有相对顺序排在后面：只重排，绝不增删。
    return head.concat(blocks.filter(block => !placed.has(block.id)));
  }));
}

/* ════════════════════════════ 二、拖拽排序 ════════════════════════════ */
// 纵向列表的拖拽排序（指针事件：鼠标与触屏通用）+ 键盘排序。拖拽过程中不改 DOM，只给行打 dragging / 落点标记；
// 松手时才一次性回调，所以拖到一半被 Esc 取消不会留下痕迹。

const DRAG_THRESHOLD_PX = 3;

/** 指针 Y 对应的“被拖项最终落在第几位”（0 起）：指针越过几条其余行的中线就排在第几位。 */
function computeInsertIndex(pointerY, rects, fromIndex) {
  if (!Array.isArray(rects) || fromIndex < 0 || fromIndex >= rects.length || !Number.isFinite(pointerY)) return fromIndex;
  let passed = 0;
  rects.forEach((rect, index) => {
    if (index === fromIndex) return;
    if (pointerY >= (rect.top + rect.bottom) / 2) passed += 1;
  });
  return passed;
}

/** 落点指示线画在哪一行的哪一侧（索引是原列表里的位置）；只有一行或索引非法时为 null。 */
function dropMarkFor(count, fromIndex, toIndex) {
  if (count < 2 || fromIndex < 0 || fromIndex >= count || toIndex < 0 || toIndex >= count) return null;
  const others = [];
  for (let i = 0; i < count; i += 1) if (i !== fromIndex) others.push(i);
  if (toIndex >= others.length) return { index: lastOf(others), side: 'after' };
  return { index: others[toIndex], side: 'before' };
}

/** 把 id 挪到新的位置 toIndex（挪完之后它所在的下标）；id 不存在或位置没变时原样返回副本。 */
function moveIdToIndex(ids, id, toIndex) {
  const list = [...ids];
  const from = list.indexOf(id);
  if (from < 0) return list;
  const to = Math.max(0, Math.min(list.length - 1, Math.trunc(toIndex)));
  if (to === from) return list;
  list.splice(from, 1);
  list.splice(to, 0, id);
  return list;
}

/** 一组一起拖时，整组最终插在“组外的行”里第几位（0 起）。 */
function computeGroupInsertIndex(pointerY, rects, groupIndexes) {
  if (!Array.isArray(rects) || !Array.isArray(groupIndexes) || !Number.isFinite(pointerY)) return 0;
  const inGroup = new Set(groupIndexes);
  let passed = 0;
  rects.forEach((rect, index) => {
    if (inGroup.has(index)) return;
    if (pointerY >= (rect.top + rect.bottom) / 2) passed += 1;
  });
  return passed;
}

function dropMarkForGroup(count, groupIndexes, insertAt) {
  const inGroup = new Set(groupIndexes);
  const others = [];
  for (let i = 0; i < count; i += 1) if (!inGroup.has(i)) others.push(i);
  if (!others.length || insertAt < 0) return null;
  if (insertAt >= others.length) return { index: lastOf(others), side: 'after' };
  return { index: others[insertAt], side: 'before' };
}

/** 把一组 id 整体挪到“组外那些 id”的第 insertAt 位；组内保持原有先后顺序。 */
function moveGroupToIndex(ids, groupIds, insertAt) {
  const inGroup = new Set(groupIds);
  const group = ids.filter(id => inGroup.has(id));
  if (!group.length) return [...ids];
  const rest = ids.filter(id => !inGroup.has(id));
  const at = Math.max(0, Math.min(rest.length, Math.trunc(Number(insertAt)) || 0));
  return [...rest.slice(0, at), ...group, ...rest.slice(at)];
}

/** 可排序列表控制器。事件委托在 container 上，行被整体重绘后无需重新绑定。 */
function createSortableList({
  container, rowSelector, handleSelector, getId, onReorder, document: doc,
  moveStep = null, getGroupIds = null, moveGroupStep = null, autoScroll = null,
  draggingClass = 'dragging', beforeClass = 'drop-before', afterClass = 'drop-after', bodyClass = 'sortable-dragging',
} = {}) {
  if (!container || !doc) throw new TypeError('缺少 container / document');
  let drag = null; // { pointerId, id, startY, lastY, moved, handle, group }
  const removers = [];

  const rowsOf = () => Array.from(container.querySelectorAll(rowSelector));
  const idsOf = rows => rows.map(row => String(getId(row)));
  // 这一行是否带着一组一起动：getGroupIds 返回含自己的多项时才算。
  const groupOf = (id) => {
    const group = getGroupIds ? getGroupIds(id) : null;
    return Array.isArray(group) && group.length > 1 && group.map(String).includes(id) ? group.map(String) : null;
  };

  function clearMarks() {
    for (const row of rowsOf()) row.classList.remove(draggingClass, beforeClass, afterClass);
  }

  /** 按指针当前位置实时读一遍行矩形；被拖行已不在列表里时返回 null。 */
  function locate(pointerY) {
    const rows = rowsOf();
    const ids = idsOf(rows);
    const fromIndex = ids.indexOf(drag.id);
    if (fromIndex < 0) return null;
    const rects = rows.map(row => row.getBoundingClientRect());
    if (drag.group) {
      const groupIndexes = ids.map((id, index) => (drag.group.includes(id) ? index : -1)).filter(index => index >= 0);
      return { rows, ids, fromIndex, groupIndexes, insertAt: computeGroupInsertIndex(pointerY, rects, groupIndexes), toIndex: -1 };
    }
    return { rows, ids, fromIndex, toIndex: computeInsertIndex(pointerY, rects, fromIndex) };
  }

  function listen(target, type, handler, capture) {
    target.addEventListener(type, handler, capture);
    return () => target.removeEventListener(type, handler, capture);
  }

  function finish({ cancelled }) {
    if (!drag) return;
    const { moved, lastY } = drag;
    const found = cancelled || !moved ? null : locate(lastY);
    drag = null;
    removers.splice(0).forEach(off => off());
    if (doc.body) doc.body.classList.remove(bodyClass);
    clearMarks();
    if (!found) return;
    if (found.groupIndexes) {
      const group = found.groupIndexes.map(index => found.ids[index]);
      const next = moveGroupToIndex(found.ids, group, found.insertAt);
      if (next.every((id, index) => id === found.ids[index])) return;
      onReorder(next, { group });
      return;
    }
    if (found.toIndex === found.fromIndex) return;
    onReorder(moveIdToIndex(found.ids, found.ids[found.fromIndex], found.toIndex));
  }

  function onPointerMove(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    drag.lastY = event.clientY;
    if (Math.abs(event.clientY - drag.startY) > DRAG_THRESHOLD_PX) drag.moved = true;
    if (!drag.moved) return;
    if (autoScroll) autoScroll(event);
    // 自动滚动之后再量：落点要按滚动后的实时位置算。
    const found = locate(drag.lastY);
    if (!found) { finish({ cancelled: true }); return; }
    for (const row of found.rows) row.classList.remove(beforeClass, afterClass);
    const mark = found.groupIndexes
      ? dropMarkForGroup(found.rows.length, found.groupIndexes, found.insertAt)
      : dropMarkFor(found.rows.length, found.fromIndex, found.toIndex);
    if (mark) found.rows[mark.index].classList.add(mark.side === 'before' ? beforeClass : afterClass);
  }

  function onPointerDown(event) {
    if (drag) return;
    if (event.button !== undefined && event.button !== 0) return;
    const handle = event.target && event.target.closest ? event.target.closest(handleSelector) : null;
    const row = handle ? handle.closest(rowSelector) : null;
    if (!handle || !row || !container.contains(row)) return;
    event.preventDefault();
    const id = String(getId(row));
    drag = { pointerId: event.pointerId, id, startY: event.clientY, lastY: event.clientY, moved: false, handle, group: groupOf(id) };
    try { if (handle.setPointerCapture) handle.setPointerCapture(event.pointerId); } catch (error) { /* 捕获失败不影响拖拽：事件监听挂在 document 上 */ }
    if (doc.body) doc.body.classList.add(bodyClass);
    for (const item of rowsOf()) {
      if (item === row || (drag.group && drag.group.includes(String(getId(item))))) item.classList.add(draggingClass);
    }
    removers.push(
      listen(doc, 'pointermove', onPointerMove),
      listen(doc, 'pointerup', (e) => {
        if (!drag || e.pointerId !== drag.pointerId) return;
        drag.lastY = Number.isFinite(e.clientY) ? e.clientY : drag.lastY;
        finish({ cancelled: false });
      }),
      listen(doc, 'pointercancel', (e) => { if (drag && e.pointerId === drag.pointerId) finish({ cancelled: true }); }),
      // Esc：在捕获阶段拦下，只取消这次拖拽，不再传给页面里其它的 Esc 处理。
      listen(doc, 'keydown', (e) => {
        if (e.key !== 'Escape') return;
        e.preventDefault();
        e.stopPropagation();
        finish({ cancelled: true });
      }, true),
    );
  }

  function onKeyDown(event) {
    if (drag || !moveStep || (event.key !== 'ArrowUp' && event.key !== 'ArrowDown')) return;
    const handle = event.target && event.target.closest ? event.target.closest(handleSelector) : null;
    const row = handle ? handle.closest(rowSelector) : null;
    if (!handle || !row || !container.contains(row)) return;
    event.preventDefault();
    const ids = idsOf(rowsOf());
    const id = String(getId(row));
    const group = moveGroupStep ? groupOf(id) : null;
    const direction = event.key === 'ArrowUp' ? -1 : 1;
    const next = group ? moveGroupStep(ids, group, direction) : moveStep(ids, id, direction);
    if (!Array.isArray(next) || next.length !== ids.length || next.every((item, index) => item === ids[index])) return; // 边界处不动
    onReorder(next, group ? { group } : undefined);
    // 调用方重绘后旧手柄已被换掉：把焦点还给同一行的手柄，便于连续按键。
    const moved = rowsOf().find(item => String(getId(item)) === id);
    const movedHandle = moved ? moved.querySelector(handleSelector) : null;
    if (movedHandle && movedHandle.focus) movedHandle.focus();
    if (moved && moved.scrollIntoView) moved.scrollIntoView({ block: 'nearest' });
  }

  const offs = [listen(container, 'pointerdown', onPointerDown), listen(container, 'keydown', onKeyDown)];
  return {
    isDragging: () => !!drag,
    cancel: () => finish({ cancelled: true }),
    destroy() { finish({ cancelled: true }); offs.splice(0).forEach(off => off()); },
  };
}

/* ════════════════════════════ 三、编辑器界面 ════════════════════════════ */
// 只用 createElement / textContent / 属性赋值，不拼 HTML 字符串，用户文本不会被当成标记解析。
// 重绘规则（输入法与焦点）：input / change 只改数据和原地更新文字，绝不重建 DOM；
// 只有结构变化（添加 / 删除 / 移动 / 恢复默认 / 展开收起）才重建列表，且重建前后会还原焦点与选区。
// 展开（手风琴，同一时间最多编辑一条）与选择（多选 / Ctrl / Shift 点击行头）只存在于本页面，不进布局数据。

const PREVIEW_LENGTH = 40;
const EMPTY_TITLE = '（空消息）';
const ROW_SELECTOR = '.lwb-ple-row';
const HANDLE_SELECTOR = '[data-ple-drag]';
const ROLE_BADGE = Object.freeze({ system: '系统', user: '用户', assistant: '模型', tool: '工具返回' });
const TOOL_ROLE_HINT = '工具返回：对上一条工具调用的回复（不是工具定义）';
// 点这些控件时即使按着 Ctrl / Shift 也不当作“选择行”。
const MODIFIER_IGNORE_SELECTOR = 'textarea, select, input[type="text"], [data-ple-field="toggle"], [data-ple-field="select"], [data-ple-field="edit"], [data-ple-field="delete"]';
const COLLAPSE_PREFIX = 'clonoth-draw-list-collapsed:';
// 能就地编辑的内置块：块 id → /prompts 的 key。frame / limits 由程序生成，tools 是协议占位，只读。
const EDITABLE_BUILTINS = Object.freeze({ opening: 'opening', content: 'content', guide: 'guide', rules: 'rules' });
// 后端链预览里每段的 key（plugins/draw/planner/assembly.py 的 SECTION_KEYS）→ 块 id。
const BLOCK_BY_SECTION_KEY = Object.freeze({ topSystem: 'opening', frame: 'frame', toolDeclaration: 'tools', content: 'content', tagGuide: 'guide', sceneRules: 'rules', limits: 'limits' });
const TOOLS_SUMMARY = '原生工具协议时整段删除；文本协议时替换为工具说明';
const CONTENT_HINT = '必须包含 {{characterInfo}}、{{drawRequest}} 两个占位符。';
const PREVIEW_DEBOUNCE_MS = 400;

/** 标题里的内容预览：取第一个非空行，压成单行并截断。 */
function previewText(content, limit = PREVIEW_LENGTH) {
  const firstLine = String(content == null ? '' : content).split(/\r?\n/).map(line => line.trim()).find(Boolean) || '';
  const chars = Array.from(firstLine);
  return chars.length > limit ? `${chars.slice(0, limit).join('')}…` : firstLine;
}

/** 排序后哪一项被移动了：新旧顺序里各自拿掉某一项后其余相同的那一项。 */
function findMovedId(oldIds, newIds) {
  const at = oldIds.findIndex((id, index) => id !== newIds[index]);
  if (at < 0) return null;
  const same = (a, b) => a.length === b.length && a.every((id, index) => id === b[index]);
  for (const candidate of [oldIds[at], newIds[at]]) {
    if (same(oldIds.filter(id => id !== candidate), newIds.filter(id => id !== candidate))) return candidate;
  }
  return null;
}

/** 从后端链预览里取出内置块的组装文本与固定消息原文。 */
function digestChain(chain) {
  const out = { builtin: {}, action: '', calls: {}, approvalResult: '', approvalRendered: '' };
  for (const item of Array.isArray(chain) ? chain : []) {
    if (!item || item.role === 'tools') continue;
    for (const section of item.sections || []) {
      const content = String(section.content == null ? '' : section.content);
      if (section.fixed) {
        if (item.role === 'user') out.action = content;
        else if (item.role === 'assistant' && item.stageId) out.calls[item.stageId] = content;
        else if (item.role === 'tool' && item.stageId === 'approval') {
          out.approvalResult = content;
          out.approvalRendered = typeof section.rendered === 'string' ? section.rendered : '';
        }
      } else if (section.builtin && BLOCK_BY_SECTION_KEY[section.key]) {
        out.builtin[BLOCK_BY_SECTION_KEY[section.key]] = content;
      }
    }
  }
  return out;
}

function el(doc, tag, { className, text, attrs } = {}, children = []) {
  const node = doc.createElement(tag);
  if (className) node.setAttribute('class', className);
  if (text !== undefined) node.textContent = String(text);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children) if (child) node.appendChild(child);
  return node;
}

function clearNode(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function setHidden(node, hidden) { if (hidden) node.setAttribute('hidden', ''); else node.removeAttribute('hidden'); }
function setDisabled(node, disabled) { node.disabled = Boolean(disabled); if (disabled) node.setAttribute('disabled', ''); else node.removeAttribute('disabled'); }
const blocksOf = layout => (layout && Array.isArray(layout.blocks) ? layout.blocks : normalizeLayout(layout).layout.blocks);

// 笔形图标（与 LWB character-action-icons.js 的 edit 同形）。
function penIcon(doc) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = doc.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  for (const d of ['M12 20h9', 'M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z']) {
    const path = doc.createElementNS(NS, 'path');
    path.setAttribute('d', d);
    svg.appendChild(path);
  }
  return svg;
}

function mount(container, options) {
  if (!container) throw new TypeError('缺少 container');
  const opts = options || {};
  const doc = container.ownerDocument;
  const view = doc.defaultView;
  const api = typeof opts.api === 'function' ? opts.api : null;
  const toast = typeof opts.say === 'function' ? opts.say : () => {};
  const ask = typeof opts.confirm === 'function' ? opts.confirm : text => view.confirm(text);
  const prompts = opts.prompts && typeof opts.prompts.get === 'function' ? opts.prompts : null;
  const onChange = typeof opts.onChange === 'function' ? opts.onChange : () => {};
  const random = typeof opts.random === 'function' ? opts.random : Math.random;
  const collapseId = String(opts.collapseId || 'prompt-layout');
  const create = (tag, spec, children) => el(doc, tag, spec, children);

  let ownLayout = createDefaultLayout();
  let ownSaved = ownLayout; // setLayout 最近一次设置的值：判断“未保存”徽标的基准
  const readLayout = () => ownLayout;

  const expanded = new Set(); // 正在编辑的块 id（最多一个）
  const previewOpen = new Set(); // 已展开预览的行 id
  const previewSources = new Map(); // 行 id → 取预览原文的函数
  const fixedSummaries = new Map(); // 固定消息行 id → 取行内灰字的函数
  const selected = new Set(); // 选中的自定义块 id
  let selectMode = false;
  let anchorId = null; // Shift 选范围的起点
  let lastSelectedId = null; // 最近一次选中的块：没有展开项时，新消息插在它下方
  let undoState = null; // { layout } 删除 / 批量复制前的整份布局
  let pendingFocus = null; // 重建后要聚焦的 { id, field }
  let sortable = null;
  let destroyed = false;
  const listeners = [];

  let previewData = digestChain([]);
  let previewLoaded = false;
  let previewSeq = 0;
  let previewTimer = 0;

  function readCollapsed() {
    try { return view.localStorage.getItem(COLLAPSE_PREFIX + collapseId) === '1'; } catch (error) { return false; }
  }
  function writeCollapsed(value) {
    try {
      if (value) view.localStorage.setItem(COLLAPSE_PREFIX + collapseId, '1');
      else view.localStorage.removeItem(COLLAPSE_PREFIX + collapseId);
    } catch (error) { /* 存储不可用时只在本次页面内生效 */ }
  }
  let collapsed = readCollapsed();

  // ── 外壳：只建一次，列表在里面重建，状态行（aria-live）因此不会被换掉 ──
  const root = create('div', { className: 'lwb-ple', attrs: { 'data-ple': '' } });
  const collapseButton = create('button', { className: 'lwb-ple-collapse', attrs: { type: 'button', 'data-ple-collapse': '', title: '折叠 / 展开' } }, [
    create('span', { className: 'lwb-ple-collapse-icon', text: '▾', attrs: { 'aria-hidden': 'true' } }),
  ]);
  const collapsedCount = create('span', { className: 'lwb-ple-collapsed-count', attrs: { hidden: true, 'data-ple-collapsed-count': '' } });
  const titleNode = create('span', { className: 'lwb-ple-title', text: '请求结构' });
  const dirtyBadge = create('span', { className: 'lwb-ple-dirty', text: '未保存', attrs: { hidden: true, 'data-ple-dirty': '' } });
  const addButton = create('button', { className: 'lwb-ple-btn lwb-ple-btn-primary', text: '添加消息', attrs: { type: 'button', 'data-ple-add': '', title: '在当前展开的消息下方添加一条自定义消息（没有展开项时，加在最近选中的消息下方，再没有就加在末尾）' } });
  const selectModeButton = create('button', { className: 'lwb-ple-btn', text: '多选', attrs: { type: 'button', 'data-ple-select-mode': '', 'aria-pressed': 'false', title: '显示选择框；也可以按住 Ctrl / Shift 点击消息行选择（Shift 选范围）' } });
  const resetButton = create('button', { className: 'lwb-ple-btn', text: '恢复默认结构', attrs: { type: 'button', 'data-ple-reset': '', title: '恢复内置块的默认顺序并移除自定义消息（只改草稿，保存后才生效）' } });
  const hint = create('p', { className: 'lwb-ple-hint', text: '主流程固定，自定义消息可放在最前、各环节之间或最后，也可以拖动调整。结构改动点“保存结构”后生效；内置块的文本在各自的行里单独保存。' });
  const selectionCount = create('span', { className: 'lwb-ple-selection-count', attrs: { 'data-ple-selection-count': '' } });
  const copyButton = create('button', { className: 'lwb-ple-btn lwb-ple-btn-small', text: '复制', attrs: { type: 'button', 'data-ple-bulk': 'copy', title: '复制选中的自定义消息，副本放在原消息下方' } });
  const bulkDeleteButton = create('button', { className: 'lwb-ple-btn lwb-ple-btn-small lwb-ple-btn-danger', text: '删除', attrs: { type: 'button', 'data-ple-bulk': 'delete', title: '删除选中的自定义消息' } });
  const bulkEnableButton = create('button', { className: 'lwb-ple-btn lwb-ple-btn-small', text: '停用', attrs: { type: 'button', 'data-ple-bulk': 'enable' } });
  const clearSelectionButton = create('button', { className: 'lwb-ple-btn lwb-ple-btn-small', text: '取消选择', attrs: { type: 'button', 'data-ple-bulk': 'clear' } });
  const selectionNote = create('span', { className: 'lwb-ple-selection-note', attrs: { 'data-ple-selection-note': '' } });
  const selectionBar = create('div', { className: 'lwb-ple-selection-bar', attrs: { hidden: true, 'data-ple-selection-bar': '' } }, [
    selectionCount, copyButton, bulkDeleteButton, bulkEnableButton, clearSelectionButton, selectionNote,
  ]);
  const list = create('div', { className: 'lwb-ple-list', attrs: { role: 'list', 'aria-label': '请求结构' } });
  const undoText = create('span', { className: 'lwb-ple-undo-text', attrs: { 'data-ple-undo-text': '' } });
  const undoButton = create('button', { className: 'lwb-ple-btn lwb-ple-btn-small', text: '撤销', attrs: { type: 'button', 'data-ple-undo': '' } });
  const undoClose = create('button', { className: 'lwb-ple-btn lwb-ple-btn-small', text: '×', attrs: { type: 'button', 'data-ple-undo-close': '', 'aria-label': '关闭撤销提示' } });
  const undoBar = create('div', { className: 'lwb-ple-undo-bar', attrs: { hidden: true, role: 'status', 'data-ple-undo-bar': '' } }, [undoText, undoButton, undoClose]);
  const status = create('div', { className: 'lwb-ple-status', attrs: { role: 'status', 'aria-live': 'polite', 'data-ple-status': '' } });
  // 折叠时保留内置表单节点的隐藏容器：收起、排序都不销毁输入值与事件绑定。
  const parking = create('div', { attrs: { hidden: true, 'data-ple-parking': '' } });
  root.appendChild(create('div', { className: 'lwb-ple-head' }, [
    create('div', { className: 'lwb-ple-headline' }, [collapseButton, titleNode, dirtyBadge, collapsedCount]),
    create('div', { className: 'lwb-ple-actions' }, [addButton, selectModeButton, resetButton]),
  ]));
  for (const node of [hint, selectionBar, list, undoBar, status, parking]) root.appendChild(node);

  function announce(text) { status.textContent = text; }
  function listen(target, type, handler) {
    target.addEventListener(type, handler);
    listeners.push(() => target.removeEventListener(type, handler));
  }
  const errorText = error => String((error && error.message) || error || '未知错误');

  // ── 内置块的就地编辑表单 ──

  const builtinEditors = {}; // 块 id → 表单节点
  const builtinState = {}; // 块 id → { textarea, stateNode, saveButton, resetButton, saved, isDefault, loaded, busy }

  function builtinLabel(id) { return BUILTIN_BY_ID.get(id).label; }
  function builtinDirty(id) {
    const state = builtinState[id];
    return Boolean(state && state.loaded && state.textarea.value !== state.saved);
  }

  function updateBuiltinState(id) {
    const state = builtinState[id];
    if (!state) return;
    const dirty = builtinDirty(id);
    state.stateNode.textContent = !state.loaded ? '' : dirty ? '有未保存的修改' : state.isDefault ? '当前为默认内容' : '已自定义';
    state.stateNode.setAttribute('data-dirty', String(dirty));
    setDisabled(state.saveButton, state.busy || !dirty);
    setDisabled(state.resetButton, state.busy || (state.isDefault && !dirty));
  }

  // 从宿主取已保存的文本。没有未保存的修改（或 force）时覆盖输入框；有的话只更新基准，不动用户正在写的内容。
  function syncBuiltin(id, { force = false } = {}) {
    const state = builtinState[id];
    const saved = state && prompts ? prompts.get(EDITABLE_BUILTINS[id]) : null;
    if (!state || !saved) return;
    const dirty = builtinDirty(id);
    state.saved = String(saved.content == null ? '' : saved.content);
    state.isDefault = saved.is_default !== false;
    if (!state.loaded || !dirty || force) state.textarea.value = state.saved;
    state.loaded = true;
    updateBuiltinState(id);
  }

  async function runBuiltinAction(id, action) {
    const state = builtinState[id];
    const label = builtinLabel(id);
    if (!state || state.busy) return;
    if (action === 'reset' && !ask(`把“${label}”恢复为默认内容？自定义内容和未保存的修改都会丢失。`)) return;
    state.busy = true;
    updateBuiltinState(id);
    try {
      if (action === 'save') await prompts.save(EDITABLE_BUILTINS[id], state.textarea.value);
      else await prompts.reset(EDITABLE_BUILTINS[id]);
      syncBuiltin(id, { force: true });
      announce(action === 'save' ? `已保存“${label}”` : `“${label}”已恢复默认内容`);
      refreshSummaries();
      schedulePreview(0);
    } catch (error) {
      toast(errorText(error), 'err');
      announce(`${action === 'save' ? '保存' : '恢复默认'}“${label}”失败：${errorText(error)}`);
    } finally {
      state.busy = false;
      updateBuiltinState(id);
    }
  }

  function buildBuiltinEditor(id) {
    const label = builtinLabel(id);
    const textarea = create('textarea', { className: 'lwb-ple-textarea', attrs: { rows: '12', spellcheck: 'false', 'data-ple-field': 'builtin-text', 'aria-label': `${label} 的内容` } });
    const stateNode = create('span', { className: 'lwb-ple-builtin-state', attrs: { role: 'status' } });
    const saveButton = create('button', { className: 'lwb-ple-btn lwb-ple-btn-primary', text: '保存', attrs: { type: 'button' } });
    const resetBuiltinButton = create('button', { className: 'lwb-ple-btn', text: '恢复默认', attrs: { type: 'button' } });
    builtinState[id] = { textarea, stateNode, saveButton, resetButton: resetBuiltinButton, saved: '', isDefault: true, loaded: false, busy: false };
    listen(textarea, 'input', () => { updateBuiltinState(id); refreshSummaries(); schedulePreview(); });
    listen(saveButton, 'click', () => runBuiltinAction(id, 'save'));
    listen(resetBuiltinButton, 'click', () => runBuiltinAction(id, 'reset'));
    return create('div', { className: 'lwb-ple-body lwb-ple-builtin-editor', attrs: { 'data-ple-builtin-editor': id } }, [
      create('label', { className: 'lwb-ple-field' }, [create('span', { text: '内容' }), textarea]),
      id === 'content' ? create('p', { className: 'lwb-ple-hint', text: CONTENT_HINT }) : null,
      create('div', { className: 'lwb-ple-body-actions' }, [stateNode, saveButton, resetBuiltinButton]),
    ]);
  }

  if (prompts) {
    for (const id of Object.keys(EDITABLE_BUILTINS)) {
      builtinEditors[id] = buildBuiltinEditor(id);
      parking.appendChild(builtinEditors[id]);
    }
  }

  function parkBuiltinEditors() {
    for (const editor of Object.values(builtinEditors)) parking.appendChild(editor);
  }

  /** 内置块的一行信息：summary 是行里的灰字，preview 是组装后的全文（来自后端），action 决定按钮。 */
  function builtinInfo(id) {
    const editable = Boolean(builtinEditors[id]);
    const assembled = String(previewData.builtin[id] || '');
    if (id === 'tools') return { summary: TOOLS_SUMMARY, preview: '', action: null };
    const state = builtinState[id];
    const source = editable && state.loaded ? state.textarea.value : assembled;
    return { summary: previewText(source) || (previewLoaded || editable ? '（空）' : '（加载中…）'), preview: assembled, action: editable ? 'edit' : null };
  }

  // ── 只读预览：按钮开合一块原文，不改布局 ──

  function previewControls(id, label, getText) {
    const open = previewOpen.has(id);
    previewSources.set(id, getText);
    // 预览复用可编辑消息的文本框外观（自动换行、等宽字体、可滚动、可选中复制），只是设为只读。
    const body = create('textarea', {
      className: 'lwb-ple-textarea lwb-ple-preview',
      attrs: { readonly: true, rows: '3', spellcheck: 'false', 'aria-label': `${label} 的内容（只读）`, 'data-ple-preview-body': '', hidden: !open },
    });
    body.value = open ? getText() : '';
    const button = create('button', {
      className: 'lwb-ple-btn lwb-ple-btn-small', text: open ? '收起' : '预览',
      attrs: { type: 'button', 'data-ple-preview': id, 'aria-expanded': String(open), 'aria-label': `预览：${label}` },
    });
    return { button, body };
  }

  // 预览框按内容撑高，最多 360px，更长的内容在框里滚动。测不到高度时（隐藏中）保持默认行数。
  function fitPreview(body) {
    if (!body.scrollHeight) return;
    body.style.height = 'auto';
    body.style.height = `${Math.min(body.scrollHeight + 2, 360)}px`;
  }

  function previewBodyOf(button) {
    const host = button.closest('.lwb-ple-row, .lwb-ple-fixed');
    return host ? host.querySelector('[data-ple-preview-body]') : null;
  }

  function togglePreview(button) {
    const id = button.getAttribute('data-ple-preview');
    const body = previewBodyOf(button);
    if (!body) return;
    const open = !previewOpen.has(id);
    if (open) {
      previewOpen.add(id);
      const source = previewSources.get(id);
      body.value = (source ? source() : '') || (previewLoaded ? '（空）' : '（预览加载中…）');
    } else previewOpen.delete(id);
    setHidden(body, !open);
    if (open) fitPreview(body);
    button.textContent = open ? '收起' : '预览';
    button.setAttribute('aria-expanded', String(open));
  }

  // 预览回包只更新已展开的原文和固定消息行的灰字，不重建列表、不触碰编辑草稿。
  function refreshPreviews() {
    for (const button of list.querySelectorAll('[data-ple-preview]')) {
      const id = button.getAttribute('data-ple-preview');
      if (!previewOpen.has(id)) continue;
      const body = previewBodyOf(button);
      const source = previewSources.get(id);
      const text = (source ? source() : '') || (previewLoaded ? '（空）' : '（预览加载中…）');
      if (!body || body.value === text) continue;
      body.value = text;
      fitPreview(body);
    }
    for (const node of list.querySelectorAll('[data-ple-fixed-summary]')) {
      const source = fixedSummaries.get(node.getAttribute('data-ple-fixed-summary'));
      node.textContent = previewText(source ? source() : '') || (previewLoaded ? '' : '（加载中…）');
    }
  }

  function schedulePreview(delay = PREVIEW_DEBOUNCE_MS) {
    if (!api || destroyed) return;
    view.clearTimeout(previewTimer);
    previewTimer = view.setTimeout(runPreview, delay);
  }

  function getDraftTexts() {
    const texts = {};
    for (const [id, key] of Object.entries(EDITABLE_BUILTINS)) {
      const state = builtinState[id];
      if (state && state.loaded) texts[key] = state.textarea.value;
    }
    return texts;
  }

  // 内置块的组装文本与固定消息原文只取决于提示词文本和设置，与自定义消息无关，所以只在文本变化 / setLayout 时重取；
  // 请求里带默认布局，结果就不受已保存的自定义消息影响（同角色的自定义消息会和内置块并进同一条）。
  async function runPreview() {
    if (!api || destroyed) return;
    previewSeq += 1;
    const seq = previewSeq;
    try {
      const data = await api('/preview', { layout: createDefaultLayout(), texts: getDraftTexts() });
      if (seq !== previewSeq || destroyed) return;
      previewData = digestChain(data && data.chain);
      previewLoaded = true;
      refreshPreviews();
      refreshSummaries();
    } catch (error) {
      if (seq !== previewSeq || destroyed) return;
      announce(`预览更新失败：${errorText(error)}`);
    }
  }

  // ── 分组与固定消息行 ──

  // 固定消息成组：左边是组名（也是整组的拖动把手，只支持鼠标 / 触摸），右边是行；自定义消息不进组。
  function buildGroup(label, rows, attrs, index) {
    const name = create('span', { className: 'lwb-ple-group-label', attrs: { 'data-ple-drag': attrs['data-ple-sort-id'], title: '拖动整组调整位置' } }, [
      create('span', { className: 'lwb-ple-group-grip', text: '⋮⋮', attrs: { 'aria-hidden': 'true' } }),
      create('span', { text: label }),
    ]);
    return create('section', { className: `lwb-ple-stage lwb-ple-group${index % 2 ? ' alt' : ''}`, attrs }, [
      name,
      create('div', { className: 'lwb-ple-group-rows' }, rows),
    ]);
  }

  // 行尾的按钮固定占两格：第一格“预览”，第二格“编辑 / 收起”。没有的那格留空，各行按钮因此上下对齐。
  function slot(button) {
    return create('span', { className: `lwb-ple-slot${button ? '' : ' empty'}`, attrs: button ? {} : { 'aria-hidden': 'true' } }, button ? [button] : []);
  }

  function roleBadge(role) {
    return create('span', { className: `lwb-ple-role ${role}`, text: ROLE_BADGE[role], attrs: { 'data-ple-role': '', title: role === 'tool' ? TOOL_ROLE_HINT : undefined } });
  }

  // getSummary 是行里那一小段灰字；缺省取预览全文的第一行。
  function buildFixedRow({ id, role, label, getText, getSummary = getText }) {
    const { button, body } = previewControls(id, label, getText);
    fixedSummaries.set(id, getSummary);
    return create('div', { className: 'lwb-ple-fixed', attrs: { role: 'listitem', 'data-ple-fixed': id } }, [
      create('div', { className: 'lwb-ple-line' }, [
        roleBadge(role),
        create('span', { className: 'lwb-ple-label', text: label }),
        create('span', { className: 'lwb-ple-summary', text: previewText(getSummary()) || (previewLoaded ? '' : '（加载中…）'), attrs: { 'data-ple-fixed-summary': id } }),
        slot(button),
        slot(null),
      ]),
      body,
    ]);
  }

  // ── 行 ──

  function customTitle(block) {
    const name = String(block.name || '').trim();
    return name || previewText(block.content) || EMPTY_TITLE;
  }
  const countText = block => `${String(block.content || '').length} 字`;

  function buildHandle(block, label) {
    return create('span', {
      className: 'lwb-ple-handle', text: '⋮⋮',
      attrs: { role: 'button', tabindex: '0', 'data-ple-drag': block.id, 'data-ple-field': 'handle', title: '拖动调整顺序，也可聚焦后按 ↑ / ↓', 'aria-label': `拖动调整 ${label} 的顺序` },
    });
  }

  function buildSelectBox(label) {
    return create('label', { className: 'lwb-ple-select-box' }, [
      create('input', { attrs: { type: 'checkbox', 'data-ple-field': 'select', 'aria-label': `选择 ${label}`, title: '选中（Shift 点击选范围）' } }),
    ]);
  }

  function buildBuiltinRow(block) {
    const meta = describeBlock(block);
    const info = builtinInfo(block.id);
    const label = meta.label;
    const editor = builtinEditors[block.id];
    const open = expanded.has(block.id);
    const row = create('div', {
      className: `lwb-ple-row lwb-ple-builtin${editor ? ' lwb-ple-editable' : ''}${open ? ' expanded' : ''}`,
      attrs: { role: 'listitem', 'data-block-id': block.id, 'data-builtin': 'true' },
    });
    const line = create('div', { className: 'lwb-ple-line' }, [
      roleBadge(meta.role),
      create(editor ? 'button' : 'span', {
        className: editor ? 'lwb-ple-main lwb-ple-label' : 'lwb-ple-label', text: label,
        attrs: editor ? { type: 'button', 'data-ple-field': 'main', 'aria-expanded': String(open) } : {},
      }),
      create('span', { className: 'lwb-ple-summary', text: info.summary, attrs: { 'data-ple-summary': '' } }),
    ]);
    let previewBody = null;
    let previewButton = null;
    if (block.id !== 'tools') {
      const preview = previewControls(block.id, label, () => builtinInfo(block.id).preview);
      previewButton = preview.button;
      previewBody = preview.body;
    }
    let actionButton = null;
    if (info.action) {
      const verb = open ? '收起' : '编辑';
      // 可编辑的行用笔形图标按钮；收起仍是文字按钮。
      actionButton = verb === '编辑'
        ? create('button', { className: 'lwb-ple-icon-btn', attrs: { type: 'button', 'data-ple-field': 'edit', title: verb, 'aria-label': `${verb}：${label}` } }, [penIcon(doc)])
        : create('button', { className: 'lwb-ple-btn lwb-ple-btn-small', text: verb, attrs: { type: 'button', 'data-ple-field': 'edit', 'aria-label': `${verb}：${label}` } });
    }
    line.appendChild(slot(previewButton));
    line.appendChild(slot(actionButton));
    row.appendChild(line);
    if (previewBody) row.appendChild(previewBody);
    if (editor && open) {
      if (!builtinState[block.id].loaded) syncBuiltin(block.id);
      row.appendChild(editor);
    }
    return row;
  }

  function buildCustomBody(block) {
    const roleSelect = create('select', { className: 'lwb-ple-select', attrs: { 'data-ple-field': 'role' } });
    for (const role of CUSTOM_BLOCK_ROLES) {
      const option = create('option', { text: ROLE_BADGE[role], attrs: { value: role } });
      if (role === block.role) option.setAttribute('selected', '');
      roleSelect.appendChild(option);
    }
    const nameInput = create('input', {
      className: 'lwb-ple-input',
      attrs: { type: 'text', 'data-ple-field': 'name', placeholder: '名称（可选，仅用于区分）', autocomplete: 'off' },
    });
    nameInput.value = block.name || '';
    const textarea = create('textarea', { className: 'lwb-ple-textarea', attrs: { rows: '6', 'data-ple-field': 'content', placeholder: '消息内容' } });
    textarea.value = block.content || '';
    return create('div', { className: 'lwb-ple-body' }, [
      create('div', { className: 'lwb-ple-fields' }, [
        create('label', { className: 'lwb-ple-field' }, [create('span', { text: '角色' }), roleSelect]),
        create('label', { className: 'lwb-ple-field lwb-ple-field-grow' }, [create('span', { text: '名称' }), nameInput]),
      ]),
      create('label', { className: 'lwb-ple-field' }, [create('span', { text: '内容' }), textarea]),
      create('p', { className: 'lwb-ple-hint', text: '相邻且角色相同的消息在发送时会用空行合并成一条；内容为空或已停用的消息不发送。' }),
      create('div', { className: 'lwb-ple-body-actions' }, [
        create('button', { className: 'lwb-ple-btn lwb-ple-btn-danger', text: '删除此消息', attrs: { type: 'button', 'data-ple-field': 'delete' } }),
      ]),
    ]);
  }

  function buildCustomRow(block) {
    const open = expanded.has(block.id);
    const title = customTitle(block);
    const row = create('div', {
      className: `lwb-ple-row lwb-ple-custom${open ? ' expanded' : ''}${block.enabled ? '' : ' off'}`,
      attrs: { role: 'listitem', 'data-block-id': block.id, 'data-builtin': 'false', 'data-ple-sort-id': block.id },
    });
    const toggle = create('input', { attrs: { type: 'checkbox', 'data-ple-field': 'toggle', 'aria-label': `启用 ${title}`, title: '关闭后这条消息不会发送，但内容保留' } });
    toggle.checked = block.enabled;
    if (block.enabled) toggle.setAttribute('checked', '');
    const main = create('button', {
      className: 'lwb-ple-main',
      attrs: { type: 'button', 'data-ple-field': 'main', 'aria-expanded': open ? 'true' : 'false', title: open ? '收起' : '展开编辑' },
    }, [
      create('span', { className: `lwb-ple-label${String(block.name || '').trim() ? '' : ' muted'}`, text: title, attrs: { 'data-ple-title': '' } }),
      create('span', { className: 'lwb-ple-count', text: countText(block), attrs: { 'data-ple-count': '' } }),
      create('span', { className: 'lwb-ple-chevron', text: open ? '▾' : '▸', attrs: { 'aria-hidden': 'true' } }),
    ]);
    row.appendChild(create('div', { className: 'lwb-ple-line' }, [
      buildHandle(block, title),
      buildSelectBox(title),
      roleBadge(block.role),
      create('label', { className: 'lwb-ple-toggle' }, [toggle]),
      main,
    ]));
    if (open) row.appendChild(buildCustomBody(block));
    return row;
  }

  // 输入时只改这一行的文字，不重建（保住输入法组字和焦点）。
  function patchCustomRow(row, block) {
    const title = customTitle(block);
    const hasName = Boolean(String(block.name || '').trim());
    const titleNodeInRow = row.querySelector('[data-ple-title]');
    if (titleNodeInRow) {
      titleNodeInRow.textContent = title;
      titleNodeInRow.setAttribute('class', `lwb-ple-label${hasName ? '' : ' muted'}`);
    }
    const count = row.querySelector('[data-ple-count]');
    if (count) count.textContent = countText(block);
    const badge = row.querySelector('[data-ple-role]');
    if (badge) {
      badge.setAttribute('class', `lwb-ple-role ${block.role}`);
      badge.textContent = ROLE_BADGE[block.role];
    }
    row.classList.toggle('off', !block.enabled);
    const labels = { '[data-ple-field="toggle"]': `启用 ${title}`, '[data-ple-drag]': `拖动调整 ${title} 的顺序`, '[data-ple-field="select"]': `选择 ${title}` };
    for (const [selector, text] of Object.entries(labels)) {
      const node = row.querySelector(selector);
      if (node) node.setAttribute('aria-label', text);
    }
  }

  // ── 焦点保持 ──

  function captureFocus() {
    const active = doc.activeElement;
    if (!active || !list.contains(active)) return null;
    const row = active.closest(ROW_SELECTOR);
    const field = active.getAttribute('data-ple-field');
    if (!row || !field) return null;
    return { id: row.getAttribute('data-block-id'), field, start: active.selectionStart, end: active.selectionEnd };
  }

  function restoreFocus(target) {
    if (!target) return;
    const row = Array.from(list.querySelectorAll(ROW_SELECTOR)).find(item => item.getAttribute('data-block-id') === target.id);
    const node = row && Array.from(row.querySelectorAll('[data-ple-field]')).find(item => item.getAttribute('data-ple-field') === target.field);
    if (!node) return;
    node.focus();
    if (typeof target.start === 'number' && typeof node.setSelectionRange === 'function') {
      try { node.setSelectionRange(target.start, target.end == null ? target.start : target.end); } catch (error) { /* 复选框等控件没有选区 */ }
    }
  }

  // ── 重建与刷新 ──

  function rebuildList() {
    const layout = readLayout();
    const blocks = blocksOf(layout);
    const focusTarget = pendingFocus || captureFocus();
    pendingFocus = null;
    const known = new Set(blocks.map(block => block.id));
    for (const id of Array.from(expanded)) if (!known.has(id)) expanded.delete(id);
    for (const id of Array.from(selected)) if (!known.has(id)) selected.delete(id);
    if (!known.has(anchorId)) anchorId = null;
    if (!known.has(lastSelectedId)) lastSelectedId = null;
    parkBuiltinEditors();
    clearNode(list);
    previewSources.clear();
    fixedSummaries.clear();
    const stages = projectLayout(layout);
    for (const block of stages[0].leadingBlocks) list.appendChild(buildCustomRow(block));
    stages.forEach((stage, index) => {
      const rows = [];
      if (stage.id === 'materials') rows.push(buildFixedRow({ id: 'fixed-action', role: 'user', label: '执行步骤', getText: () => previewData.action }));
      if (stage.id !== 'setup') {
        rows.push(buildFixedRow({ id: `fixed-${stage.id}-call`, role: 'assistant', label: `${PREBUILT_MESSAGE_LABELS.call}：${stage.label}`, getText: () => previewData.calls[stage.id] || '' }));
      }
      rows.push(...stage.blockIds.map(id => buildBuiltinRow({ id })));
      list.appendChild(buildGroup(stage.label, rows, { 'data-ple-sort-id': lastOf(stage.blockIds), 'data-ple-stage': stage.id }, index));
      for (const block of stage.customBlocks) list.appendChild(buildCustomRow(block));
    });
    list.appendChild(buildGroup('提交确认', [
      buildFixedRow({ id: 'fixed-approval-call', role: 'assistant', label: `${PREBUILT_MESSAGE_LABELS.call}：申请提交`, getText: () => previewData.calls.approval || '' }),
      // 预览显示真正发出去的内容（工具返回消息是带 approved 的 JSON 包装，由后端给出）；行里的灰字只放包装里面那句话。
      buildFixedRow({
        id: 'fixed-approval-result', role: 'tool', label: `${PREBUILT_MESSAGE_LABELS.result}：确认提交`,
        getText: () => previewData.approvalRendered || previewData.approvalResult, getSummary: () => previewData.approvalResult,
      }),
    ], { 'data-ple-sort-id': APPROVAL_MARKER_ID, 'data-ple-stage': 'approval' }, stages.length));
    for (const block of lastOf(stages).trailingBlocks) list.appendChild(buildCustomRow(block));
    for (const body of list.querySelectorAll('[data-ple-preview-body]:not([hidden])')) fitPreview(body);
    refreshStatus();
    refreshSelectionUi();
    restoreFocus(focusTarget);
  }

  const layoutDirty = () => layoutFingerprint(readLayout()) !== layoutFingerprint(ownSaved);

  /** 未保存徽标、按钮可用性——不动列表。 */
  function refreshStatus() {
    setHidden(dirtyBadge, !layoutDirty());
    setDisabled(resetButton, isDefaultLayout(readLayout()));
    syncCollapse();
  }

  // ── 整个面板的折叠 ──

  function syncCollapse() {
    root.classList.toggle('collapsed', collapsed);
    collapseButton.classList.toggle('collapsed', collapsed);
    collapseButton.setAttribute('aria-expanded', String(!collapsed));
    collapseButton.setAttribute('aria-label', `${collapsed ? '展开' : '折叠'}请求结构`);
    collapsedCount.textContent = `共 ${blocksOf(readLayout()).length} 条`;
    setHidden(collapsedCount, !collapsed);
  }

  function setCollapsed(value) {
    const next = Boolean(value);
    if (next === collapsed) return;
    collapsed = next;
    writeCollapsed(collapsed);
    syncCollapse();
    announce(collapsed ? '已折叠请求结构' : '已展开请求结构');
  }

  /** 内置块的摘要随文本框内容 / 预览回包变化：原地改字，不重建。 */
  function refreshSummaries() {
    for (const row of list.querySelectorAll(ROW_SELECTOR)) {
      if (row.getAttribute('data-builtin') !== 'true') continue;
      const node = row.querySelector('[data-ple-summary]');
      if (node) node.textContent = builtinInfo(row.getAttribute('data-block-id')).summary;
    }
  }

  // 拖到视口上下边缘时滚动页面（面板随窗口滚动）。
  function autoScroll(event) {
    const edge = 48;
    if (event.clientY < edge) view.scrollBy(0, -14);
    else if (event.clientY > view.innerHeight - edge) view.scrollBy(0, 14);
  }

  // 固定组在排序列表里用它最后一块的 id 当把手；换回成员块 id，布局操作才认得。
  function expandStageIds(ids) {
    return ids.flatMap((id) => {
      const stage = STAGES.find(item => lastOf(item.blockIds) === id);
      return stage ? stage.blockIds : [id];
    });
  }

  function sortableIds(layout) {
    const stages = projectLayout(layout);
    return [
      ...stages[0].leadingBlocks.map(block => block.id),
      ...stages.flatMap(stage => [lastOf(stage.blockIds), ...stage.customBlocks.map(block => block.id)]),
      APPROVAL_MARKER_ID,
      ...lastOf(stages).trailingBlocks.map(block => block.id),
    ];
  }

  // ── 用户操作 ──

  function commit(next, { rebuild = true } = {}) {
    clearUndo();
    ownLayout = next;
    onChange(next);
    if (rebuild) rebuildList(); else refreshStatus();
  }

  function labelOf(block) {
    const meta = describeBlock(block);
    return meta.builtin ? meta.label : customTitle(block);
  }

  const orderedIds = () => blocksOf(readLayout()).map(block => block.id);
  const selectedInOrder = () => orderedIds().filter(id => selected.has(id));
  const selectedCustomIds = () => blocksOf(readLayout()).filter(block => selected.has(block.id) && !describeBlock(block).builtin).map(block => block.id);
  const rowOf = id => Array.from(list.querySelectorAll(ROW_SELECTOR)).find(item => item.getAttribute('data-block-id') === id) || null;

  // ── 展开（手风琴）──

  function expandOnly(id) {
    expanded.clear();
    if (id) expanded.add(id);
  }

  function toggleExpanded(id) {
    if (expanded.has(id)) expanded.delete(id); else expandOnly(id);
    pendingFocus = { id, field: 'main' };
    rebuildList();
  }

  // ── 选择 ──

  function refreshSelectionUi() {
    root.classList.toggle('select-mode', selectMode);
    selectModeButton.setAttribute('aria-pressed', selectMode ? 'true' : 'false');
    for (const row of list.querySelectorAll(ROW_SELECTOR)) {
      const on = selected.has(row.getAttribute('data-block-id'));
      row.classList.toggle('selected', on);
      const box = row.querySelector('[data-ple-field="select"]');
      if (box) {
        box.checked = on;
        if (on) box.setAttribute('checked', ''); else box.removeAttribute('checked');
      }
    }
    const customSelected = blocksOf(readLayout()).filter(block => selected.has(block.id) && !describeBlock(block).builtin);
    setHidden(selectionBar, selected.size === 0);
    selectionCount.textContent = `已选 ${selected.size} 项`;
    const noCustom = customSelected.length === 0;
    const anyEnabled = customSelected.some(block => block.enabled);
    bulkEnableButton.textContent = anyEnabled || noCustom ? '停用' : '启用';
    bulkEnableButton.title = anyEnabled ? '停用选中的自定义消息（内容保留，但不发送）' : '启用选中的自定义消息';
    for (const button of [copyButton, bulkDeleteButton, bulkEnableButton]) setDisabled(button, noCustom);
    if (selected.size === 0) selectionNote.textContent = '';
  }

  function clearSelection({ leaveMode = false } = {}) {
    selected.clear();
    anchorId = null;
    lastSelectedId = null;
    if (!leaveMode) selectMode = false;
    selectionNote.textContent = '';
    refreshSelectionUi();
  }

  function afterSelectionChange() {
    if (selected.size > 0) selectMode = true;
    selectionNote.textContent = '';
    refreshSelectionUi();
    announce(selected.size ? `已选 ${selected.size} 项` : '已取消选择');
  }

  function selectOne(id, on = null) {
    if (describeBlock({ id }).builtin) return;
    const shouldAdd = on === null ? !selected.has(id) : on;
    if (shouldAdd) {
      selected.add(id);
      lastSelectedId = id;
    } else {
      selected.delete(id);
      if (lastSelectedId === id) lastSelectedId = lastOf(selectedInOrder()) || null;
    }
    anchorId = id;
    afterSelectionChange();
  }

  function selectRangeTo(id) {
    const ids = blocksOf(readLayout()).filter(block => !describeBlock(block).builtin).map(block => block.id);
    const from = ids.indexOf(anchorId == null ? id : anchorId);
    const to = ids.indexOf(id);
    if (to < 0) return;
    const [lo, hi] = from < 0 ? [to, to] : [Math.min(from, to), Math.max(from, to)];
    for (const item of ids.slice(lo, hi + 1)) selected.add(item);
    lastSelectedId = id;
    if (anchorId === null) anchorId = id;
    afterSelectionChange();
  }

  function toggleSelectMode() {
    setCollapsed(false); // 折叠时看不到选择框：先展开
    if (selectMode) {
      clearSelection();
      announce('已退出多选');
    } else {
      selectMode = true;
      refreshSelectionUi();
      announce('已进入多选：点选择框，或按住 Shift 点击选范围');
    }
  }

  // ── 撤销条 ──

  function clearUndo() {
    undoState = null;
    setHidden(undoBar, true);
    undoText.textContent = '';
  }

  function showUndo(text, previousLayout) {
    undoState = { layout: previousLayout };
    undoText.textContent = text;
    setHidden(undoBar, false);
  }

  function handleUndo() {
    if (!undoState) return;
    commit(undoState.layout);
    announce('已撤销，布局恢复到操作之前');
  }

  // ── 添加 / 恢复 / 删除 / 排序 ──

  /** 新消息插在谁的下方：当前展开的消息 → 最近选中的消息 → 末尾（null）。 */
  function insertionAnchorId(blocks) {
    const known = new Set(blocks.map(block => block.id));
    const open = Array.from(expanded).find(id => known.has(id));
    if (open) return open;
    if (lastSelectedId && known.has(lastSelectedId)) return lastSelectedId;
    return lastOf(selectedInOrder()) || null;
  }

  function handleAdd() {
    setCollapsed(false); // 折叠时新消息会加在看不见的地方：先展开
    const layout = readLayout();
    const blocks = blocksOf(layout);
    const anchorBlock = blocks.find(block => block.id === insertionAnchorId(blocks)) || null;
    const block = createCustomBlock({ role: 'user' }, blocks.map(item => item.id), random);
    expandOnly(block.id);
    pendingFocus = { id: block.id, field: 'content' };
    commit(addBlockAfter(layout, block, anchorBlock ? anchorBlock.id : undefined));
    const row = rowOf(block.id);
    if (row && row.scrollIntoView) row.scrollIntoView({ block: 'nearest' });
    const total = list.querySelectorAll(ROW_SELECTOR).length;
    announce(anchorBlock
      ? `已在“${labelOf(anchorBlock)}”下方添加一条自定义消息，共 ${total} 项`
      : `已在末尾添加一条自定义消息，共 ${total} 项`);
  }

  function handleReset() {
    const layout = readLayout();
    const customCount = blocksOf(layout).filter(block => !describeBlock(block).builtin).length;
    if (customCount > 0 && !ask(`恢复默认结构会移除全部 ${customCount} 条自定义消息。\n只改当前草稿，点击“保存结构”后才会生效。确定吗？`)) return;
    expanded.clear();
    clearSelection({ leaveMode: true });
    commit(createDefaultLayout());
    showUndo(customCount > 0 ? `已恢复默认结构，移除了 ${customCount} 条自定义消息` : '已恢复默认结构', layout);
    addButton.focus();
    announce(customCount > 0 ? `已恢复默认结构，移除了 ${customCount} 条自定义消息` : '已恢复默认结构');
  }

  function handleDelete(id) {
    const layout = readLayout();
    const blocks = blocksOf(layout);
    const index = blocks.findIndex(block => block.id === id);
    const block = blocks[index];
    if (!block || describeBlock(block).builtin) return;
    if (String(block.content || '').trim() && !ask(`确定删除“${customTitle(block)}”吗？其中的内容会一并丢失。`)) return;
    const name = customTitle(block);
    expanded.delete(id);
    selected.delete(id);
    // 删完后焦点落到原位置的下一项（没有就上一项）的手柄，键盘用户不至于丢到页面顶部。
    const neighbor = blocks.slice(index + 1).find(item => !describeBlock(item).builtin)
      || blocks.slice(0, index).reverse().find(item => !describeBlock(item).builtin);
    pendingFocus = neighbor ? { id: neighbor.id, field: 'handle' } : null;
    commit(removeBlock(layout, id));
    if (!neighbor) addButton.focus();
    showUndo(`已删除“${name}”`, layout);
    announce(`已删除“${name}”`);
  }

  function handleBulkCopy() {
    const layout = readLayout();
    const { layout: next, copies, skipped } = duplicateBlocks(layout, selectedInOrder(), random);
    if (!copies.length) {
      selectionNote.textContent = '选中的都是内置块，内置块不能复制';
      announce(selectionNote.textContent);
      return;
    }
    selected.clear();
    for (const copy of copies) selected.add(copy.id);
    lastSelectedId = lastOf(copies).id;
    anchorId = lastSelectedId;
    commit(next);
    const skippedText = skipped.length ? `；跳过 ${skipped.length} 个内置块（内置块不能复制）` : '';
    selectionNote.textContent = skipped.length ? `跳过 ${skipped.length} 个内置块` : '';
    showUndo(`已复制 ${copies.length} 条消息${skippedText}`, layout);
    announce(`已复制 ${copies.length} 条消息，副本放在原消息下方并已选中${skippedText}`);
  }

  function handleBulkDelete() {
    const layout = readLayout();
    const { layout: next, removedIds, skipped } = removeBlocks(layout, selectedInOrder());
    if (!removedIds.length) {
      selectionNote.textContent = '选中的都是内置块，内置块不能删除';
      announce(selectionNote.textContent);
      return;
    }
    for (const id of removedIds) {
      selected.delete(id);
      expanded.delete(id);
    }
    commit(next);
    const skippedText = skipped.length ? `；${skipped.length} 个内置块保留` : '';
    selectionNote.textContent = skipped.length ? `${skipped.length} 个内置块保留` : '';
    showUndo(`已删除 ${removedIds.length} 条消息${skippedText}`, layout);
    announce(`已删除 ${removedIds.length} 条消息${skippedText}`);
  }

  function handleBulkEnable() {
    const ids = selectedCustomIds();
    if (!ids.length) return;
    const layout = readLayout();
    const anyEnabled = blocksOf(layout).some(block => ids.includes(block.id) && block.enabled);
    commit(setBlocksEnabled(layout, ids, !anyEnabled));
    announce(anyEnabled ? `已停用 ${ids.length} 条消息` : `已启用 ${ids.length} 条消息`);
  }

  function handleReorder(orderedIdsNext, meta) {
    // 新旧顺序放在同一个 id 空间里比（排序列表的 id：固定组用最后一块的 id，另含“提交确认”标记），才能找出谁被移动了。
    const oldIds = sortableIds(readLayout());
    const next = reorderBlocks(readLayout(), expandStageIds(orderedIdsNext));
    const nextIds = blocksOf(next).map(block => block.id);
    commit(next);
    if (meta && Array.isArray(meta.group) && meta.group.length > 1) {
      const positions = meta.group.map(id => nextIds.indexOf(id) + 1).filter(position => position > 0);
      announce(`已移动 ${meta.group.length} 项，现在位于第 ${Math.min(...positions)}–${Math.max(...positions)} 位，共 ${nextIds.length} 位`);
      return;
    }
    const movedId = findMovedId(oldIds, orderedIdsNext);
    const movedStage = STAGES.find(stage => lastOf(stage.blockIds) === movedId);
    if (movedId === APPROVAL_MARKER_ID) announce('“提交确认”已移动位置');
    else if (movedStage) announce(`“${movedStage.label}”整组已移动位置`);
    else if (movedId) {
      const blocks = blocksOf(next);
      const block = blocks.find(item => item.id === movedId);
      const position = blocks.findIndex(item => item.id === movedId) + 1;
      announce(`“${block ? labelOf(block) : movedId}”已移到第 ${position} 位，共 ${nextIds.length} 位`);
    }
  }

  function handlePatch(id, patch, row) {
    const next = updateBlock(readLayout(), id, patch);
    commit(next, { rebuild: false });
    const block = blocksOf(next).find(item => item.id === id);
    if (block && row) patchCustomRow(row, block);
  }

  function fieldOf(event) {
    const node = event.target && event.target.closest ? event.target.closest('[data-ple-field]') : null;
    const row = node ? node.closest(ROW_SELECTOR) : null;
    if (!node || !row || !list.contains(row)) return null;
    return { node, row, field: node.getAttribute('data-ple-field'), id: row.getAttribute('data-block-id') };
  }

  listen(list, 'click', (event) => {
    const target = event.target;
    if (!target || !target.closest) return;
    const previewButton = target.closest('[data-ple-preview]');
    if (previewButton) { togglePreview(previewButton); return; }
    // Ctrl / ⌘ / Shift 点击行头 = 选择（Shift 选范围），不触发展开、编辑等原有操作。
    const modifier = event.ctrlKey || event.metaKey || event.shiftKey;
    const row = target.closest(ROW_SELECTOR);
    if (modifier && row && list.contains(row) && target.closest('.lwb-ple-line') && !target.closest(MODIFIER_IGNORE_SELECTOR)) {
      event.preventDefault();
      const id = row.getAttribute('data-block-id');
      if (event.shiftKey) selectRangeTo(id); else selectOne(id);
      return;
    }
    const hit = fieldOf(event);
    if (!hit) return;
    if (hit.field === 'select') {
      if (event.shiftKey) selectRangeTo(hit.id); else selectOne(hit.id, Boolean(hit.node.checked));
    } else if (hit.field === 'main') toggleExpanded(hit.id);
    else if (hit.field === 'edit') { if (builtinEditors[hit.id]) toggleExpanded(hit.id); }
    else if (hit.field === 'delete') handleDelete(hit.id);
  });
  listen(list, 'input', (event) => {
    const hit = fieldOf(event);
    if (!hit) return;
    if (hit.field === 'name') handlePatch(hit.id, { name: hit.node.value }, hit.row);
    else if (hit.field === 'content') handlePatch(hit.id, { content: hit.node.value }, hit.row);
  });
  listen(list, 'change', (event) => {
    const hit = fieldOf(event);
    if (!hit) return;
    if (hit.field === 'toggle') handlePatch(hit.id, { enabled: Boolean(hit.node.checked) }, hit.row);
    else if (hit.field === 'role') handlePatch(hit.id, { role: hit.node.value }, hit.row);
  });
  listen(collapseButton, 'click', () => setCollapsed(!collapsed));
  listen(addButton, 'click', handleAdd);
  listen(resetButton, 'click', handleReset);
  listen(selectModeButton, 'click', toggleSelectMode);
  listen(copyButton, 'click', handleBulkCopy);
  listen(bulkDeleteButton, 'click', handleBulkDelete);
  listen(bulkEnableButton, 'click', handleBulkEnable);
  listen(clearSelectionButton, 'click', () => { clearSelection({ leaveMode: true }); announce('已取消选择'); });
  listen(undoButton, 'click', handleUndo);
  listen(undoClose, 'click', clearUndo);

  clearNode(container);
  container.appendChild(root);
  sortable = createSortableList({
    container: list,
    rowSelector: '[data-ple-sort-id]',
    handleSelector: HANDLE_SELECTOR,
    getId: row => row.getAttribute('data-ple-sort-id'),
    onReorder: handleReorder,
    moveStep: (_ids, id, direction) => sortableIds(moveBlocks(readLayout(), [id], direction)),
    // 拖动 / 键盘移动选中的行之一时，整组一起动。
    getGroupIds: id => (selected.size > 1 && selected.has(id) ? selectedInOrder() : null),
    moveGroupStep: (_ids, group, direction) => sortableIds(moveBlocks(readLayout(), group, direction)),
    autoScroll,
    document: doc,
    draggingClass: 'dragging',
    beforeClass: 'drop-before',
    afterClass: 'drop-after',
    bodyClass: 'lwb-ple-dragging',
  });
  rebuildList();

  return {
    /** 设置布局（同时当作“已保存”的基准）并重绘；内置文本没有未保存修改的会跟着宿主刷新。 */
    setLayout(layout) {
      clearUndo();
      ownLayout = normalizeLayout(layout).layout;
      ownSaved = ownLayout;
      for (const id of Object.keys(builtinEditors)) syncBuiltin(id);
      rebuildList();
      schedulePreview(0);
    },
    /** 当前草稿（已规范化，名称去掉首尾空格）。 */
    getLayout: () => normalizeLayout(readLayout()).layout,
    isDirty: layoutDirty,
    hasUnsavedText: () => Object.keys(builtinEditors).some(builtinDirty),
    getDraftTexts,
    /** 宿主在别处改了提示词文本后调用：没有未保存修改的输入框跟着更新。 */
    refreshBuiltinTexts() {
      for (const id of Object.keys(builtinEditors)) syncBuiltin(id);
      refreshSummaries();
      schedulePreview(0);
    },
    refreshPreview: () => schedulePreview(0),
    isCollapsed: () => collapsed,
    setCollapsed,
    destroy() {
      destroyed = true;
      view.clearTimeout(previewTimer);
      if (sortable) sortable.destroy();
      sortable = null;
      listeners.splice(0).forEach(off => off());
      if (root.parentNode) root.parentNode.removeChild(root);
    },
  };
}

window.DrawLayoutEditor = {
  mount,
  previewText,
  findMovedId,
  digestChain,
  contract: {
    LAYOUT_VERSION, APPROVAL_MARKER_ID, BUILTIN_BLOCKS, STAGES, BUILTIN_BLOCK_IDS, CUSTOM_BLOCK_ROLES,
    createDefaultLayout, createCustomBlock, normalizeLayout, layoutFingerprint, isDefaultLayout, projectLayout, describeBlock,
    addBlockAfter, duplicateBlocks, removeBlocks, setBlocksEnabled, moveBlocks, removeBlock, updateBlock, reorderBlocks,
  },
  sortable: { computeInsertIndex, dropMarkFor, moveIdToIndex, computeGroupInsertIndex, dropMarkForGroup, moveGroupToIndex, createSortableList },
};
})();
