/* 绘图管理面板逻辑。API 前缀 /v1/plugins/draw，token 取宿主 window.parent.__CLONOTH_BOOT__。 */
(() => {
'use strict';
const BASE = '/v1/plugins/draw';
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const splitCsv = s => String(s || '').split(/[,，]/).map(x => x.trim()).filter(Boolean);
const num = (id, d) => { const v = $(id).value; return v === '' ? d : Number(v); };

function token() { try { const b = window.parent.__CLONOTH_BOOT__; if (b && b.token) return b.token; } catch (e) {} return ''; }
async function api(path, body, method) {
  const t = token();
  const resp = await fetch(BASE + path, {
    method: method || (body === undefined ? 'GET' : 'POST'),
    headers: { 'Content-Type': 'application/json', ...(t ? { Authorization: 'Bearer ' + t } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await resp.json(); } catch (e) {}
  if (!resp.ok || data.ok === false) {
    const err = new Error(data.message || data.detail || ('HTTP ' + resp.status));
    err.details = data.details; throw err;
  }
  return data.data;
}
let msgTimer = 0;
function say(text, kind) {
  const el = $('msg'); el.textContent = text; el.className = 'show ' + (kind || '');
  clearTimeout(msgTimer); msgTimer = setTimeout(() => { el.className = ''; }, kind === 'err' ? 9000 : 4000);
}
const fail = e => say(e.message || String(e), 'err');
function imageUrl(path) { return BASE + '/images/file/' + encodeURIComponent(path.split('/').pop()); }
async function fetchImageBlob(path) {
  const t = token();
  const resp = await fetch(imageUrl(path), { headers: t ? { Authorization: 'Bearer ' + t } : {} });
  if (!resp.ok) throw new Error('HTTP ' + resp.status);
  return URL.createObjectURL(await resp.blob());
}

/* ── 导航 ── */
const loaded = {};
const loaders = {};
function show(view) {
  document.querySelectorAll('nav.side a').forEach(a => a.classList.toggle('active', a.dataset.view === view));
  document.querySelectorAll('section.view').forEach(s => s.classList.toggle('active', s.id === 'view-' + view));
  location.hash = view;
  if (!loaded[view] && loaders[view]) { loaded[view] = true; loaders[view]().catch(fail); }
}
$('nav').addEventListener('click', e => { const a = e.target.closest('a[data-view]'); if (a) show(a.dataset.view); });

/* ── 状态 ── */
const S = { settings: null, presets: { presets: [], selected_preset_id: '' }, pIndex: 0, chars: [], cIndex: 0, cQuery: '', images: [], prompts: null, prKey: 'layout', fields: null, layout: null,
  agent: null, agIndex: 0, agSaved: '', agEffective: { ok: false, text: '' } };

/* ═══ API 配置 ═══ */
loaders.api = async () => {
  S.settings = await api('/settings');
  const s = S.settings, a = s.api || {}, p = s.planner || {}, g = s.generation || {}, st = s.storage || {};
  $('api-hint').innerHTML = s.api_key_configured ? '<b>key 已配置</b>' : '<b style="color:var(--danger)">key 未配置</b>' + (s.is_private ? '' : '（当前使用 example 模板，保存后生成私有 settings.yaml）');
  $('a-key').value = ''; $('a-base').value = a.base_url || ''; $('a-timeout').value = a.timeout_sec ?? ''; $('a-keyenv').value = a.api_key_env || '';
  $('a-attempts').value = p.max_attempts ?? 3; $('a-custom').value = p.max_custom_tool_calls ?? 5;
  $('a-maximg').value = p.max_plan_images ?? 4;
  $('a-sendeach').checked = p.send_each_image !== false;
  $('g-retry').value = g.retry_max_attempts ?? 5; $('g-wait').value = g.retry_wait_sec ?? 3; $('g-delay').value = g.request_delay_sec ?? 0;
  $('g-lretry').value = g.lock_retry_max_attempts ?? 8; $('g-lmin').value = g.lock_retry_wait_min_sec ?? 6; $('g-lmax').value = g.lock_retry_wait_max_sec ?? 8;
  $('s-enabled').checked = st.cleanup_enabled !== false; $('s-days').value = st.retention_days ?? 7; $('s-mb').value = st.max_total_mb ?? 2048; $('s-interval').value = st.cleanup_interval_sec ?? 3600;
};
$('a-reload').onclick = () => loaders.api().then(() => say('已重新加载')).catch(fail);
$('a-save').onclick = async () => {
  const patch = {
    api: { base_url: $('a-base').value.trim(), timeout_sec: num('a-timeout', 130), api_key_env: $('a-keyenv').value.trim() || 'NOVELAI_API_KEY' },
    planner: { max_attempts: num('a-attempts', 3), max_custom_tool_calls: num('a-custom', 5),
               max_plan_images: num('a-maximg', 4), send_each_image: $('a-sendeach').checked },
    generation: { retry_max_attempts: num('g-retry', 5), retry_wait_sec: num('g-wait', 3), request_delay_sec: num('g-delay', 0),
                  lock_retry_max_attempts: num('g-lretry', 8), lock_retry_wait_min_sec: num('g-lmin', 6), lock_retry_wait_max_sec: num('g-lmax', 8) },
    storage: { cleanup_enabled: $('s-enabled').checked, retention_days: num('s-days', 7), max_total_mb: num('s-mb', 2048), cleanup_interval_sec: num('s-interval', 3600) },
  };
  const key = $('a-key').value.trim(); if (key) patch.api.api_key = key;
  try { S.settings = await api('/settings', patch); await loaders.api(); say('已保存', 'ok'); } catch (e) { fail(e); }
};
$('a-clear').onclick = async () => {
  if (!confirm('清空 settings.yaml 里保存的 API key？（环境变量 / .env 不受影响）')) return;
  try { await api('/settings', { clear_api_key: true }); await loaders.api(); say('已清空', 'ok'); } catch (e) { fail(e); }
};

/* ═══ 场景 Agent（规划模型的接口预设） ═══ */
// [key, 标签, min, max, step, 默认值]；每个 Provider 实际发送哪几项由后端返回的 providers[].sampling 决定。
const SAMPLING_ROWS = [['temperature', '温度', 0, 2, 0.05, 1], ['top_p', 'Top P', 0, 1, 0.01, 1], ['top_k', 'Top K', 1, '', 1, 40],
  ['frequency_penalty', '频率惩罚', -2, 2, 0.05, 0], ['presence_penalty', '存在惩罚', -2, 2, 0.05, 0], ['repetition_penalty', '重复惩罚', 0, '', 0.05, 1]];
const agModels = {}; // 预设 id -> 已拉取的模型，只在内存里
const agCur = () => (S.agent ? S.agent.presets[S.agIndex] : null);
const agSnapshot = () => JSON.stringify([S.agent.presets, S.agent.current_preset_id]);
const agDirty = () => !!S.agent && agSnapshot() !== S.agSaved;
function agChannel(id) { return ((S.agent && S.agent.channels) || []).find(c => c.id === id) || null; }
function agChannelLabel(p) { const c = agChannel(p.channel); return c ? c.name : (p.channel ? p.channel + '（不存在）' : '未选择渠道方案'); }
function agModelOf(p) { const c = agChannel(p.channel); return p.model || (c && c.model) || ''; }
function agProvider(id) {
  return ((S.agent && S.agent.providers) || []).find(x => x.id === id)
    || { id, label: id || '未选择', sampling: [], list_auth: [], default_base_url: '', sampling_note: '', reasoning_note: '' };
}
function agNewId() { return 'agent-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }
function agUniqueName(base) { const names = new Set(S.agent.presets.map(p => p.name)); let name = base; for (let i = 2; names.has(name); i++) name = base + ' ' + i; return name; }
function blankAgent() {
  const sampling = {}; SAMPLING_ROWS.forEach(r => { sampling[r[0]] = { enabled: false, value: r[5] }; });
  return { id: agNewId(), name: agUniqueName('新预设'), channel: ((S.agent.channels || [])[0] || {}).id || '', model: '',
    max_tokens: 0, timeout_sec: 180, tool_mode: 'native', sampling, reasoning: { mode: 'inherit', effort: '' } };
}
// 后端返回的整份数据就是“已保存”的状态：顶部的生效说明按它来写，未保存的草稿不影响这句话。
function agAdopt(data, keepId) {
  S.agent = data;
  S.agIndex = Math.max(0, data.presets.findIndex(p => p.id === (keepId || data.current_preset_id)));
  S.agSaved = agSnapshot();
  const cur = data.presets.find(p => p.id === data.current_preset_id) || data.presets[0];
  const eff = data.effective || {};
  S.agEffective = eff.source === 'preset'
    ? { ok: true, text: `规划正在使用预设「${cur.name}」：${agChannelLabel(cur)} / ${agModelOf(cur) || '方案未设模型'}` }
    : { ok: false, text: `预设「${cur.name}」${eff.reason || '未选择渠道方案'}，规划使用节点 draw.novelai_planner 的渠道（主设置 → 节点模型）。` };
  renderAgent();
}
loaders.agent = async () => agAdopt(await api('/agent'));
function renderAgentList() {
  const A = S.agent;
  $('ag-list').innerHTML = A.presets.map((p, i) => `<div class="item ${i === S.agIndex ? 'active' : ''}" data-i="${i}"><span class="name" title="${esc(p.name)}">${esc(p.name || '（未命名）')}</span><span class="meta"><span class="sub">${esc(agChannelLabel(p) + ' / ' + (agModelOf(p) || '方案未设模型'))}</span>${p.id === A.current_preset_id ? '<span class="tag">使用中</span>' : ''}</span></div>`).join('');
  $('ag-dirty').textContent = agDirty() ? '有未保存的改动' : '';
}
// 跟着 Provider / Reasoning 变的部分：地址占位、模型列表鉴权、思考强度、采样参数行。切换 Provider 不改任何已填的值。
function renderAgentProvider() {
  const p = agCur(), ch = agChannel(p.channel), info = agProvider(ch ? ch.type : '');
  $('ag-model').placeholder = ch && ch.model ? `留空用方案的模型：${ch.model}` : '留空用方案的模型';
  $('ag-effort-wrap').style.display = ((p.reasoning || {}).mode || 'inherit') === 'on' ? '' : 'none';
  $('ag-rnote').textContent = info.reasoning_note || '';
  const rows = SAMPLING_ROWS.filter(r => (info.sampling || []).includes(r[0]));
  $('ag-samp').innerHTML = rows.map(([k, label, min, max, step, dflt]) => {
    const s = (p.sampling || {})[k] || { enabled: false, value: dflt };
    return `<div class="samp-row" data-samp="${k}"><label><input type="checkbox" ${s.enabled ? 'checked' : ''}><span>${label}</span></label><input type="number" min="${min}" max="${max}" step="${step}" value="${esc(s.value)}" ${s.enabled ? '' : 'disabled'}></div>`;
  }).join('') || '<span class="hint">这个 Provider 不在这里发送采样参数，用接口默认。</span>';
  $('ag-snote').textContent = rows.length ? (info.sampling_note || '') : '';
}
function renderAgentModels() {
  const p = agCur(), models = (p && agModels[p.id]) || [], sel = $('ag-pulled');
  sel.style.display = models.length ? '' : 'none';
  sel.innerHTML = '<option value="">从已拉取的模型里选…</option>' + models.map(m => `<option value="${esc(m)}" ${p && m === p.model ? 'selected' : ''}>${esc(m)}</option>`).join('');
}
function renderAgent() {
  const A = S.agent, p = agCur();
  const eff = $('ag-effective'); eff.textContent = S.agEffective.text; eff.className = 'card status ' + (S.agEffective.ok ? 'ok' : 'warn');
  if (document.activeElement !== $('ag-retries')) $('ag-retries').value = A.planner_max_retries;
  renderAgentList();
  $('ag-form').style.display = p ? '' : 'none'; if (!p) return;
  const isCurrent = p.id === A.current_preset_id;
  $('ag-name').value = p.name || ''; $('ag-current').checked = isCurrent; $('ag-current').disabled = isCurrent;
  const chs = A.channels || [];
  $('ag-channel').innerHTML = '<option value="">（不接管，用节点自身的渠道）</option>'
    + chs.map(c => `<option value="${esc(c.id)}">${esc(c.name)}（${esc(c.type)}${c.model ? ' / ' + esc(c.model) : ''}）</option>`).join('')
    + (p.channel && !agChannel(p.channel) ? `<option value="${esc(p.channel)}">${esc(p.channel)}（不存在）</option>` : '');
  $('ag-channel').value = p.channel || '';
  $('ag-model').value = p.model || ''; $('ag-maxtok').value = p.max_tokens ?? 0; $('ag-timeout').value = p.timeout_sec ?? 180;
  $('ag-tool').value = p.tool_mode || 'native'; $('ag-rmode').value = (p.reasoning || {}).mode || 'inherit';
  $('ag-effort').innerHTML = '<option value="">跟随模型默认</option>' + (A.reasoning_efforts || []).map(e => `<option value="${esc(e)}">${esc(e)}</option>`).join('');
  $('ag-effort').value = (p.reasoning || {}).effort || '';
  for (const id of ['ag-result', 'ag-pull-state']) { $(id).textContent = ''; $(id).className = 'hint'; }
  renderAgentProvider(); renderAgentModels();
}
function agInt(id, dflt) { const v = Math.floor(num(id, dflt)); return Number.isFinite(v) && v >= 0 ? v : dflt; }
function collectAgent() {
  const p = agCur(); if (!p) return;
  p.name = $('ag-name').value.trim(); p.channel = $('ag-channel').value; p.model = $('ag-model').value.trim();
  p.max_tokens = agInt('ag-maxtok', 0); p.timeout_sec = agInt('ag-timeout', 180); p.tool_mode = $('ag-tool').value;
  p.reasoning = { mode: $('ag-rmode').value, effort: $('ag-effort').value };
  p.sampling = p.sampling || {};
  document.querySelectorAll('#ag-samp [data-samp]').forEach(row => {
    const k = row.dataset.samp, v = row.querySelector('input[type=number]').value, dflt = SAMPLING_ROWS.find(r => r[0] === k)[5];
    p.sampling[k] = { enabled: row.querySelector('input[type=checkbox]').checked, value: v === '' || !Number.isFinite(Number(v)) ? dflt : Number(v) };
  });
  if ($('ag-current').checked) S.agent.current_preset_id = p.id;
}
$('ag-list').onclick = e => { const it = e.target.closest('.item'); if (!it) return; collectAgent(); S.agIndex = Number(it.dataset.i); renderAgent(); };
$('ag-add').onclick = () => { collectAgent(); S.agent.presets.push(blankAgent()); S.agIndex = S.agent.presets.length - 1; renderAgent(); };
$('ag-dup').onclick = () => {
  collectAgent(); const p = agCur(); if (!p) return;
  const c = JSON.parse(JSON.stringify(p)); c.id = agNewId(); c.name = agUniqueName((p.name || '预设') + ' 副本');
  S.agent.presets.splice(S.agIndex + 1, 0, c); S.agIndex++; renderAgent();
};
$('ag-del').onclick = () => {
  const A = S.agent, p = agCur(); if (!p) return;
  if (A.presets.length <= 1) return say('至少要保留一套预设', 'err');
  if (!confirm(`删除预设「${p.name}」？点“保存全部预设”后生效。`)) return;
  A.presets.splice(S.agIndex, 1); S.agIndex = Math.min(S.agIndex, A.presets.length - 1);
  if (A.current_preset_id === p.id) A.current_preset_id = A.presets[S.agIndex].id;
  renderAgent();
};
$('ag-pulled').onchange = () => { const v = $('ag-pulled').value; if (v) $('ag-model').value = v; };
$('ag-form').addEventListener('change', e => {
  const row = e.target.closest('[data-samp]');
  if (row) row.querySelector('input[type=number]').disabled = !row.querySelector('input[type=checkbox]').checked;
  collectAgent();
  if (e.target.id === 'ag-channel' || e.target.id === 'ag-rmode') renderAgentProvider();
  if (e.target.id === 'ag-current') $('ag-current').disabled = $('ag-current').checked;
  renderAgentList();
});
$('ag-save').onclick = async () => {
  collectAgent(); const keep = agCur() && agCur().id;
  try { agAdopt(await api('/agent', { presets: S.agent.presets, current_preset_id: S.agent.current_preset_id }), keep); say('已保存', 'ok'); } catch (e) { fail(e); }
};
// “拉取模型 / 测试连接”用表单里还没保存的内容，地址和 key 取自所选渠道方案。
async function agCall(btn, stateEl, path, busyText, done) {
  collectAgent(); const p = agCur(); if (!p) return;
  btn.disabled = true; stateEl.className = 'hint'; stateEl.textContent = busyText;
  let text, ok = false;
  try { [text, ok] = done(await api(path, { preset: p }), p); } catch (e) { text = e.message || String(e); }
  btn.disabled = false;
  if (agCur() !== p) return; // 等结果时已经切到别的预设
  stateEl.textContent = text; stateEl.className = 'hint ' + (ok ? 'ok' : 'bad');
}
$('ag-pull').onclick = () => agCall($('ag-pull'), $('ag-pull-state'), '/agent/models', '正在拉取模型列表…', (r, p) => {
  agModels[p.id] = r.models || []; if (agCur() === p) renderAgentModels();
  return [`已拉取 ${agModels[p.id].length} 个模型`, true];
});
$('ag-test').onclick = () => agCall($('ag-test'), $('ag-result'), '/agent/test', '正在测试连接…', r => (r.ok
  ? [`连接正常：${r.provider} / ${r.model}，${r.elapsed_ms} ms，回复“${r.reply || '（空）'}”`, true]
  : [`连接失败：${r.error}`, false]));
// 重试次数改了就保存；留空或不是数字时恢复原值，不当成 0。
$('ag-retries').onchange = async () => {
  const el = $('ag-retries'), v = Number(el.value), saved = S.agent.planner_max_retries;
  if (el.value.trim() === '' || !Number.isFinite(v)) { el.value = saved; return; }
  const n = Math.min(10, Math.max(0, Math.floor(v))); el.value = n;
  try { S.agent.planner_max_retries = (await api('/agent', { planner_max_retries: n })).planner_max_retries; say('重试次数已保存', 'ok'); }
  catch (e) { el.value = saved; fail(e); }
};
window.addEventListener('beforeunload', e => { if (agDirty()) { e.preventDefault(); e.returnValue = ''; } });

/* ═══ 绘图参数（预设） ═══ */
// 下拉选项、数值范围、“新预设”的初始内容都由后端给出（plugins/draw/presets.py），和保存时的校验是同一份。
const P_TEXT = { 'p-id': 'id', 'p-name': 'name', 'p-pos': 'positive_prefix', 'p-neg': 'negative_prefix' };
const P_NUMS = { 'p-steps': 'steps', 'p-scale': 'scale', 'p-seed': 'seed', 'p-rescale': 'cfg_rescale' };
const P_LIMITS = { 'p-maximg': 'max_images', 'p-maxchar': 'max_characters_per_image' };
const P_BOOLS = { 'p-quality': 'qualityToggle', 'p-smea': 'autoSmea', 'p-variety': 'variety_boost', 'p-sm': 'sm', 'p-smdyn': 'sm_dyn', 'p-decrisper': 'decrisper', 'p-transparent': 'transparentBackground' };
const P_PICKS = { 'p-model': ['model', 'models'], 'p-sampler': ['sampler', 'samplers'], 'p-scheduler': ['scheduler', 'schedulers'] };
S.pSaved = '';
const pOpt = () => S.presets.options || {};
const pCur = () => S.presets.presets[S.pIndex];
const pSnapshot = () => JSON.stringify([S.presets.presets, S.presets.selected_preset_id]);
const pDirty = () => !!S.pSaved && pSnapshot() !== S.pSaved;
const pNewId = () => 'preset-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
// 出图时按 id / 名称找预设不分大小写，所以查重也不分。
function pUnique(key, base) {
  const used = new Set(S.presets.presets.map(p => String(p[key] || '').toLowerCase()));
  if (!used.has(base.toLowerCase())) return base;
  for (let i = 2; ; i++) { const v = base.slice(0, 60) + (key === 'id' ? '-' : ' ') + i; if (!used.has(v.toLowerCase())) return v; }
}
function pSizeValue(p) {
  if (p.override_size !== 'default') return 'auto';
  const v = `${(p.params || {}).width}x${(p.params || {}).height}`;
  return (pOpt().sizes || []).some(s => s.value === v) ? v : 'custom';
}
function pSizeText(p) { const pr = p.params || {}; return p.override_size === 'default' ? `${pr.width}×${pr.height}` : '自动尺寸'; }
function pNumber(id, prev) { const raw = $(id).value.trim(), v = Number(raw); return raw === '' || !Number.isFinite(v) ? prev : v; }
function pFillOptions() {
  const o = pOpt(), custom = '<option value="custom">自定义…</option>';
  const opts = list => (list || []).map(x => `<option value="${esc(x.value)}">${esc(x.label)}</option>`).join('');
  for (const [id, [, listKey]] of Object.entries(P_PICKS)) $(id + '-sel').innerHTML = opts(o[listKey]) + custom;
  $('p-size').innerHTML = '<option value="auto">自动（按构图：竖图 832×1216 / 横图 1216×832）</option>' + opts(o.sizes) + custom;
  $('p-uc').innerHTML = opts(o.uc_presets);
  $('p-v5-quality').innerHTML = opts(o.v5_quality_presets); $('p-v5-uc').innerHTML = opts(o.v5_uc_presets);
  const lim = o.limits || {}, dim = o.dimension || {};
  for (const [id, key] of Object.entries({ ...P_NUMS, ...P_LIMITS })) if (lim[key]) { $(id).min = lim[key][0]; $(id).max = lim[key][1]; }
  $('p-seed').min = -1; if (o.max_seed != null) $('p-seed').max = o.max_seed;
  for (const id of ['p-width', 'p-height']) { if (dim.min != null) $(id).min = dim.min; if (dim.max != null) $(id).max = dim.max; if (dim.step != null) $(id).step = dim.step; }
}
// 后端返回的整份数据就是“已保存”的状态。
function pAdopt(data, keepId) {
  S.presets = data;
  S.pIndex = Math.max(0, data.presets.findIndex(p => p.id === (keepId || data.selected_preset_id)));
  S.pSaved = pSnapshot();
  pFillOptions(); renderPresets();
}
loaders.params = async () => pAdopt(await api('/presets'));
function renderPresetList() {
  $('p-list').innerHTML = S.presets.presets.map((p, i) => `<div class="item ${i === S.pIndex ? 'active' : ''}" data-i="${i}"><span class="name" title="${esc(p.name || p.id)}">${esc(p.name || p.id)}</span><span class="meta"><span class="sub">${esc(((p.params || {}).model || '未设置模型') + ' · ' + pSizeText(p))}</span>${p.id === S.presets.selected_preset_id ? '<span class="tag">默认</span>' : ''}</span></div>`).join('');
  $('p-dirty').textContent = pDirty() ? '有未保存的改动' : '';
  $('p-up').disabled = S.pIndex <= 0; $('p-down').disabled = S.pIndex >= S.presets.presets.length - 1;
}
function pFillNumbers() {
  const p = pCur(), pr = p.params || {};
  for (const [id, k] of Object.entries(P_NUMS)) $(id).value = pr[k] ?? '';
  for (const [id, k] of Object.entries(P_LIMITS)) $(id).value = p[k] ?? 0;
  $('p-width').value = pr.width ?? ''; $('p-height').value = pr.height ?? '';
}
// 跟着模型 / 尺寸变的部分：自定义输入框、只对某些模型有效的选项、底部的提醒。隐藏的选项不改它的值。
function pSync() {
  const p = pCur(); if (!p) return;
  const o = pOpt(), pr = p.params || {}, model = String(pr.model || '');
  for (const id of Object.keys(P_PICKS)) $(id).style.display = $(id + '-sel').value === 'custom' ? '' : 'none';
  $('p-custom-size').style.display = $('p-size').value === 'custom' ? '' : 'none';
  const isV3 = model.includes('nai-diffusion-3') || model.includes('furry-3'), isV45 = model.includes('nai-diffusion-4-5'), isV5 = (o.v5_models || []).includes(model);
  document.querySelectorAll('#p-form [data-p-v3]').forEach(el => { el.style.display = isV3 ? '' : 'none'; });
  document.querySelectorAll('#p-form [data-p-v5]').forEach(el => { el.style.display = isV5 ? '' : 'none'; });
  // V5 用自己的质量词 / 负面预设，旧的开关和自动 SMEA 对它无效。
  $('p-uc-wrap').style.display = $('p-quality-wrap').style.display = isV5 ? 'none' : '';
  $('p-smea-wrap').style.display = isV3 || isV5 ? 'none' : '';
  $('p-rescale-wrap').style.display = isV3 ? 'none' : '';
  $('p-variety-wrap').style.display = isV45 ? '' : 'none';
  // Medium 是蒸馏模型：步数 / 采样器 / 重缩放 / 负面预设固定，后端保存和出图时也会强制。
  const lock = (o.locked || {})[model] || {}, lockHint = Object.keys(lock).length ? '这个模型固定：14 步、Euler Ancestral、重缩放 0、Heavy 负面' : '';
  const lockMap = { steps: 'p-steps', sampler: 'p-sampler-sel', cfg_rescale: 'p-rescale', v5UcPresetId: 'p-v5-uc' };
  for (const [k, id] of Object.entries(lockMap)) {
    const locked = lock[k] !== undefined; $(id).disabled = locked; $(id).title = locked ? lockHint : '';
    if (locked) { $(id).value = String(lock[k]); pr[k] = lock[k]; }
  }
  if (lock.sampler !== undefined) $('p-sampler').style.display = 'none';
  const notes = [], free = o.free || {}, over = [];
  if (lockHint) notes.push(lockHint + '。');
  if (p.override_size === 'default' && pr.width * pr.height > free.pixels) over.push('尺寸超过 1024×1024');
  if (pr.steps > free.steps) over.push(`步数超过 ${free.steps}`);
  if (over.length) notes.push(over.join('、') + '：超出 NovelAI 订阅免费出图的范围，出图会扣点数。');
  $('p-note').textContent = notes.join(' ');
}
function renderPresets() {
  renderPresetList();
  const p = pCur(); $('p-form').style.display = p ? '' : 'none'; if (!p) return;
  const pr = p.params || {}, o = pOpt();
  for (const [id, k] of Object.entries(P_TEXT)) $(id).value = p[k] ?? '';
  $('p-aliases').value = (p.aliases || []).join(', ');
  const isDefault = p.id === S.presets.selected_preset_id; $('p-selected').checked = isDefault; $('p-selected').disabled = isDefault;
  for (const [id, [k, listKey]] of Object.entries(P_PICKS)) {
    const known = (o[listKey] || []).some(x => x.value === pr[k]);
    $(id + '-sel').value = known ? pr[k] : 'custom'; $(id).value = known ? '' : (pr[k] ?? '');
  }
  $('p-size').value = pSizeValue(p); $('p-uc').value = String(pr.ucPreset ?? 0);
  $('p-v5-quality').value = pr.v5QualityPresetId || 'standard'; $('p-v5-uc').value = pr.v5UcPresetId || 'heavy';
  for (const [id, k] of Object.entries(P_BOOLS)) $(id).checked = !!pr[k];
  pFillNumbers(); pSync();
}
// 数字框留空或不是数字时保留原值；范围由后端在保存时检查并说明是哪一项。
function collectPreset() {
  const p = pCur(); if (!p) return;
  const wasDefault = p.id === S.presets.selected_preset_id;
  for (const [id, k] of Object.entries(P_TEXT)) p[k] = k === 'id' ? ($(id).value.trim() || p.id) : $(id).value.trim();
  p.aliases = splitCsv($('p-aliases').value);
  const pr = p.params = p.params || {};
  for (const [id, [k]] of Object.entries(P_PICKS)) pr[k] = $(id + '-sel').value === 'custom' ? $(id).value.trim() : $(id + '-sel').value;
  for (const [id, k] of Object.entries(P_NUMS)) pr[k] = pNumber(id, pr[k]);
  for (const [id, k] of Object.entries(P_LIMITS)) p[k] = pNumber(id, p[k]);
  for (const [id, k] of Object.entries(P_BOOLS)) pr[k] = $(id).checked;
  pr.ucPreset = Number($('p-uc').value);
  pr.v5QualityPresetId = $('p-v5-quality').value; pr.v5UcPresetId = $('p-v5-uc').value;
  const size = $('p-size').value;
  p.override_size = size === 'auto' ? 'auto' : 'default';
  if (size === 'custom') { pr.width = pNumber('p-width', pr.width); pr.height = pNumber('p-height', pr.height); }
  else if (size !== 'auto') [pr.width, pr.height] = size.split('x').map(Number);
  if (wasDefault || $('p-selected').checked) S.presets.selected_preset_id = p.id;
}
$('p-list').onclick = e => { const it = e.target.closest('.item'); if (!it) return; collectPreset(); S.pIndex = Number(it.dataset.i); renderPresets(); };
$('p-add').onclick = () => {
  collectPreset(); const c = JSON.parse(JSON.stringify(pOpt().blank || {}));
  c.id = pNewId(); c.name = pUnique('name', '新预设'); c.aliases = [];
  S.presets.presets.push(c); S.pIndex = S.presets.presets.length - 1; renderPresets();
};
$('p-dup').onclick = () => {
  collectPreset(); const p = pCur(); if (!p) return;
  const c = JSON.parse(JSON.stringify(p));
  // 别名不跟着复制：两套预设共用别名时，按别名找预设会有歧义。
  c.id = pUnique('id', p.id.slice(0, 59) + '-copy'); c.name = pUnique('name', (p.name || p.id).slice(0, 61) + ' 副本'); c.aliases = [];
  S.presets.presets.splice(S.pIndex + 1, 0, c); S.pIndex++; renderPresets();
};
$('p-del').onclick = () => {
  const list = S.presets.presets, p = pCur(); if (!p) return;
  if (list.length <= 1) return say('至少保留一个预设', 'err');
  if (!confirm(`删除预设「${p.name || p.id}」？点“保存全部预设”后生效。`)) return;
  list.splice(S.pIndex, 1); S.pIndex = Math.min(S.pIndex, list.length - 1);
  if (S.presets.selected_preset_id === p.id) S.presets.selected_preset_id = list[S.pIndex].id;
  renderPresets();
};
// 调整顺序：和其它改动一样是草稿，保存后生效。
function pMove(step) {
  collectPreset(); const list = S.presets.presets, j = S.pIndex + step;
  if (j < 0 || j >= list.length) return;
  [list[S.pIndex], list[j]] = [list[j], list[S.pIndex]]; S.pIndex = j; renderPresets();
}
$('p-up').onclick = () => pMove(-1);
$('p-down').onclick = () => pMove(1);
$('p-save').onclick = async () => {
  collectPreset(); const keep = pCur() && pCur().id;
  try {
    pAdopt(await api('/presets', { presets: S.presets.presets, selected_preset_id: S.presets.selected_preset_id }), keep);
    $('p-import-note').textContent = ''; say('已保存', 'ok'); loaded.test = false;
  } catch (e) { fail(e); }
};
$('p-form').addEventListener('change', e => {
  const p = pCur(); if (!p) return;
  const id = e.target.id, pr = p.params || {};
  // 切到“自定义”时先把当前值填进输入框，在它的基础上改。
  if (id.endsWith('-sel') && e.target.value === 'custom') { const base = id.slice(0, -4); $(base).value = pr[P_PICKS[base][0]] || ''; }
  if (id === 'p-size' && e.target.value === 'custom') { $('p-width').value = pr.width ?? 832; $('p-height').value = pr.height ?? 1216; }
  collectPreset(); pFillNumbers(); $('p-id').value = p.id;
  if (id === 'p-selected') $('p-selected').disabled = $('p-selected').checked;
  pSync(); renderPresetList();
});
// 导入 / 导出：小白X 的参数预设文件。导入只是加进列表，保存后才生效。
$('p-import').onclick = () => $('p-file').click();
$('p-file').onchange = async () => {
  const file = $('p-file').files[0]; $('p-file').value = ''; if (!file) return;
  let data;
  try { data = JSON.parse(await file.text()); } catch (e) { return say('这个文件不是有效的 JSON', 'err'); }
  try {
    const r = await api('/presets/import', { data });
    collectPreset(); const c = r.preset; c.id = pUnique('id', c.id); c.name = pUnique('name', c.name);
    S.presets.presets.push(c); S.pIndex = S.presets.presets.length - 1; renderPresets();
    $('p-import-note').textContent = `已导入「${c.name}」，点“保存全部预设”后生效。` + ((r.notes || []).length ? '注意：' + r.notes.join('；') + '。' : '');
  } catch (e) { fail(e); }
};
// 把一份 JSON 当文件下载（预设 / 角色导出共用）。
function downloadJson(filename, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  const a = document.createElement('a'); a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$('p-export').onclick = async () => {
  collectPreset(); const p = pCur(); if (!p) return;
  try {
    const r = await api('/presets/export', { preset: p });
    downloadJson(r.filename, r.data);
    say('已导出 ' + r.filename, 'ok');
  } catch (e) { fail(e); }
};
window.addEventListener('beforeunload', e => { if (pDirty()) { e.preventDefault(); e.returnValue = ''; } });

/* ═══ 角色标签 ═══ */
// 角色库 = 分组 + 角色。这里的改动都是草稿，点“保存全部角色”才写盘。保存时带回载入时的 revision：
// 库在别处变过（比如 QQ 里录入了角色）后端会拒绝，不会把别处的改动冲掉。清洗和校验规则在 plugins/draw/characters.py。
const C_UNGROUPED = '未分组';
const C_TYPE_LABELS = { girl: 'girl（少女）', woman: 'woman（成年女性）', boy: 'boy（少年）', man: 'man（成年男性）', other: 'other（其他）', no_humans: 'no_humans（非人主体）' };
S.groups = []; S.cFilter = 'all'; S.cRevision = ''; S.cSaved = ''; S.cMeta = { types: [], limits: {} };
const cCur = () => S.chars[S.cIndex];
const cGroup = id => S.groups.find(g => g.id === id);
const cSnapshot = () => JSON.stringify([S.groups, S.chars]);
const cDirty = () => !!S.cSaved && cSnapshot() !== S.cSaved;
const cNewId = prefix => `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`;
// 参与出图 = 角色启用，且所在分组没有关闭。
const cActive = c => c.enabled !== false && !(cGroup(c.group_id) && cGroup(c.group_id).enabled === false);
function cUniqueName(base) {
  const used = new Set(S.chars.map(c => String(c.name || '').toLowerCase()));
  if (!used.has(base.toLowerCase())) return base;
  for (let i = 2; ; i++) if (!used.has(`${base} ${i}`.toLowerCase())) return `${base} ${i}`;
}
// 保存 / 导入前的检查（名称用来在聊天里认出角色，不分大小写唯一）；后端保存时还会再查一遍。
function cProblem() {
  const seen = new Set();
  for (const c of S.chars) {
    const key = String(c.name || '').trim().toLowerCase();
    if (!key) return '有角色还没有填名称';
    if (seen.has(key)) return '角色名称重复：' + c.name;
    seen.add(key);
  }
  return '';
}
function cGroupProblem(name, self) {
  const max = (S.cMeta.limits || {}).group_name;
  if (!name) return '分组名称不能为空';
  if (name === C_UNGROUPED) return `分组不能叫“${C_UNGROUPED}”`;
  if (max && [...name].length > max) return `分组名称最多 ${max} 个字`;
  if (S.groups.some(g => g !== self && g.name.toLowerCase() === name.toLowerCase())) return '分组名称重复：' + name;
  return '';
}
function cMatches(c) {
  const f = S.cFilter;
  if (f === 'none' && c.group_id) return false;
  if (f === 'off' && cActive(c)) return false;
  if (f.startsWith('g:') && c.group_id !== f.slice(2)) return false;
  const q = S.cQuery.toLowerCase();
  return !q || [c.name, c.danbooru, c.appearance, ...(c.aliases || [])].some(x => String(x || '').toLowerCase().includes(q));
}
// 同一分组里往前 / 往后的那个角色的下标；没有返回 -1。
function cSameGroup(step) {
  const c = cCur(); if (!c) return -1;
  for (let j = S.cIndex + step; j >= 0 && j < S.chars.length; j += step) if ((S.chars[j].group_id || '') === (c.group_id || '')) return j;
  return -1;
}
function cItem(c, i) {
  const aliases = (c.aliases || []).join(' / '), g = cGroup(c.group_id);
  const off = c.enabled === false ? '停用' : g && g.enabled === false ? '分组已关闭' : '';
  const meta = aliases || off ? `<span class="meta">${aliases ? `<span class="sub" title="${esc(aliases)}">${esc(aliases)}</span>` : ''}${off ? `<span class="tag off">${off}</span>` : ''}</span>` : '';
  return `<div class="item ${i === S.cIndex ? 'active' : ''} ${off ? 'off' : ''}" data-i="${i}"><span class="name" title="${esc(c.name)}">${esc(c.name || '（未命名）')}</span>${meta}</div>`;
}
function renderCharList() {
  let html = '';
  // 只看某一个分组（或只看未分组）时不显示分组头。
  const heads = S.groups.length > 0 && S.cFilter !== 'none' && !S.cFilter.startsWith('g:');
  for (const g of [{ id: '', name: C_UNGROUPED }, ...S.groups]) {
    const rows = S.chars.map((c, i) => (c.group_id || '') === g.id && cMatches(c) ? cItem(c, i) : '').join('');
    if (!rows) continue;
    if (heads) html += `<div class="grp-head"><span>${esc(g.name)}</span>${g.enabled === false ? '<span class="tag">已关闭</span>' : ''}</div>`;
    html += rows;
  }
  $('c-list').innerHTML = html || `<div class="hint">${S.chars.length ? '没有符合条件的角色。' : '还没有角色。'}</div>`;
  $('c-stats').textContent = `共 ${S.chars.length} 个角色，${S.chars.filter(cActive).length} 个参与出图` + (S.groups.length ? `，${S.groups.length} 个分组` : '');
  $('c-dirty').textContent = cDirty() ? '有未保存的改动' : '';
  $('c-up').disabled = cSameGroup(-1) < 0; $('c-down').disabled = cSameGroup(1) < 0;
  $('c-dup').disabled = $('c-del').disabled = !cCur();
}
function cFillSelects() {
  const opts = list => list.map(([v, label]) => `<option value="${esc(v)}">${esc(label)}</option>`).join('');
  const groups = S.groups.map(g => [g.id, g.name + (g.enabled === false ? '（已关闭）' : '')]);
  $('c-filter').innerHTML = opts([['all', '全部角色'], ...(S.groups.length ? [['none', C_UNGROUPED]] : []), ...groups.map(([v, label]) => ['g:' + v, label]), ['off', '不参与出图的']]);
  if (![...$('c-filter').options].some(o => o.value === S.cFilter)) S.cFilter = 'all';
  $('c-filter').value = S.cFilter;
  $('c-group').innerHTML = opts([['', C_UNGROUPED], ...groups]);
  $('c-type-sel').innerHTML = opts([...S.cMeta.types.map(t => [t, C_TYPE_LABELS[t] || t]), ['custom', '自定义…']]);
}
function renderGroups() {
  const max = (S.cMeta.limits || {}).group_name || 32;
  $('c-groups').innerHTML = S.groups.map((g, i) => `<div class="named-row group" data-i="${i}"><input type="checkbox" class="g-on" title="关闭后，这个分组里的角色都不参与出图" ${g.enabled !== false ? 'checked' : ''}><input type="text" class="g-name" value="${esc(g.name)}" maxlength="${max}" title="分组名称，改完按回车或点别处生效"><span class="hint">${S.chars.filter(c => c.group_id === g.id).length} 个</span><button class="op small danger g-del" title="删除这个分组，里面的角色移到“${C_UNGROUPED}”">×</button></div>`).join('')
    || '<div class="hint">还没有分组。分组可以整组开关：关闭的分组里的角色都不参与出图。</div>';
}
function cNamedRows(list) {
  return (list || []).map((o, i) => `<div class="named-row" data-i="${i}"><input type="checkbox" class="n-on" title="取消勾选后，这一条不发给规划模型" ${o.enabled !== false ? 'checked' : ''}><input type="text" class="n-name" value="${esc(o.name)}" placeholder="名称"><input type="text" class="n-tags" value="${esc(o.tags)}" placeholder="英文 tag，逗号分隔"><button class="op small danger n-del" title="删除这一条">×</button></div>`).join('');
}
function cCollectNamed(id) {
  return [...$(id).querySelectorAll('.named-row')].map(el => ({ name: el.querySelector('.n-name').value.trim(), tags: el.querySelector('.n-tags').value.trim(), enabled: el.querySelector('.n-on').checked }));
}
function cSyncType() { $('c-type').style.display = $('c-type-sel').value === 'custom' ? '' : 'none'; }
function renderCharForm() {
  const c = cCur();
  $('c-form').style.display = c ? '' : 'none'; $('c-empty').style.display = c ? 'none' : '';
  if (!c) return;
  const preset = S.cMeta.types.includes(c.type);
  $('c-name').value = c.name || ''; $('c-aliases').value = (c.aliases || []).join(', ');
  $('c-type-sel').value = preset ? c.type : 'custom'; $('c-type').value = preset ? '' : c.type || ''; cSyncType();
  $('c-group').value = c.group_id || ''; $('c-enabled').checked = c.enabled !== false;
  $('c-danbooru').value = c.danbooru || ''; $('c-appearance').value = c.appearance || ''; $('c-negative').value = c.negative_tags || '';
  $('c-outfits').innerHTML = cNamedRows(c.outfits); $('c-states').innerHTML = cNamedRows(c.dynamic_states);
}
function renderChars() { cFillSelects(); renderGroups(); renderCharList(); renderCharForm(); }
// 把表单里的内容收进当前角色（草稿）。没改过的字段收回来和原来一样，不会被当成改动。
function collectChar() {
  const c = cCur(); if (!c) return;
  const aliases = splitCsv($('c-aliases').value);
  c.name = $('c-name').value.trim();
  if (aliases.join(', ') !== (c.aliases || []).join(', ')) c.aliases = aliases;
  c.type = $('c-type-sel').value === 'custom' ? ($('c-type').value.trim() || c.type) : $('c-type-sel').value;
  c.group_id = $('c-group').value; c.enabled = $('c-enabled').checked;
  c.danbooru = $('c-danbooru').value.trim(); c.appearance = $('c-appearance').value.trim(); c.negative_tags = $('c-negative').value.trim();
  c.outfits = cCollectNamed('c-outfits'); c.dynamic_states = cCollectNamed('c-states');
}
// 后端返回的整份数据就是“已保存”的状态。
function cAdopt(data, keepId) {
  S.groups = data.groups || []; S.chars = data.characters || []; S.cRevision = data.revision || '';
  S.cMeta = { types: data.type_presets || [], limits: data.limits || {} };
  S.cIndex = Math.max(0, S.chars.findIndex(c => c.id === keepId));
  S.cSaved = cSnapshot(); renderChars();
}
loaders.chars = async () => cAdopt(await api('/characters'));
$('c-search').oninput = () => { S.cQuery = $('c-search').value.trim(); renderCharList(); };
$('c-filter').onchange = () => { S.cFilter = $('c-filter').value; renderCharList(); };
$('c-list').onclick = e => { const it = e.target.closest('.item'); if (!it) return; collectChar(); S.cIndex = Number(it.dataset.i); renderCharList(); renderCharForm(); };
$('c-add').onclick = () => {
  collectChar();
  // 正在只看某个分组时，新角色直接放进这个分组。
  const group = S.cFilter.startsWith('g:') ? S.cFilter.slice(2) : '';
  S.chars.push({ id: cNewId('char'), name: cUniqueName('新角色'), enabled: true, aliases: [], type: 'girl', danbooru: '', appearance: '', negative_tags: '', outfits: [], dynamic_states: [], group_id: group });
  S.cIndex = S.chars.length - 1; S.cQuery = ''; $('c-search').value = ''; if (S.cFilter === 'off') S.cFilter = 'all';
  renderChars(); $('c-name').focus(); $('c-name').select();
};
$('c-dup').onclick = () => {
  collectChar(); const c = cCur(); if (!c) return;
  // 别名不复制：两个角色共用一个别名时，聊天里提到它就分不清是谁。
  const copy = { ...JSON.parse(JSON.stringify(c)), id: cNewId('char'), name: cUniqueName((c.name || '新角色') + ' 副本'), aliases: [] };
  S.chars.splice(S.cIndex + 1, 0, copy); S.cIndex += 1; renderChars(); $('c-name').focus();
};
$('c-del').onclick = () => {
  const c = cCur(); if (!c || !confirm(`删除角色「${c.name}」？保存后生效。`)) return;
  S.chars.splice(S.cIndex, 1); S.cIndex = Math.max(0, Math.min(S.cIndex, S.chars.length - 1)); renderChars();
};
// 调整顺序：只在所在分组里移动，和其它改动一样是草稿。
function cMove(step) {
  collectChar(); const j = cSameGroup(step); if (j < 0) return;
  [S.chars[S.cIndex], S.chars[j]] = [S.chars[j], S.chars[S.cIndex]]; S.cIndex = j; renderCharList();
}
$('c-up').onclick = () => cMove(-1);
$('c-down').onclick = () => cMove(1);
for (const [box, key, add] of [['c-outfits', 'outfits', 'c-add-outfit'], ['c-states', 'dynamic_states', 'c-add-state']]) {
  $(add).onclick = () => {
    collectChar(); const c = cCur(); if (!c) return;
    c[key].push({ name: '', tags: '', enabled: true }); renderCharForm(); renderCharList();
    const names = $(box).querySelectorAll('.n-name'); names[names.length - 1].focus();
  };
  $(box).onclick = e => {
    if (!e.target.classList.contains('n-del')) return;
    collectChar(); cCur()[key].splice(Number(e.target.closest('.named-row').dataset.i), 1); renderCharForm(); renderCharList();
  };
}
$('c-form').addEventListener('change', e => {
  const c = cCur(); if (!c) return;
  // 切到“自定义”时先把当前类型填进输入框，在它的基础上改。
  if (e.target.id === 'c-type-sel' && e.target.value === 'custom') $('c-type').value = c.type || '';
  collectChar(); cSyncType();
  if (e.target.id === 'c-group') renderGroups();
  renderCharList();
});
$('c-group-add').onclick = () => {
  const name = $('c-group-name').value.trim(), max = (S.cMeta.limits || {}).groups;
  const problem = max && S.groups.length >= max ? `分组最多 ${max} 个` : cGroupProblem(name);
  if (problem) return say(problem, 'err');
  collectChar(); S.groups.push({ id: cNewId('grp'), name, enabled: true }); $('c-group-name').value = ''; renderChars();
};
$('c-group-name').onkeydown = e => { if (e.key === 'Enter') $('c-group-add').click(); };
$('c-groups').addEventListener('change', e => {
  const row = e.target.closest('.named-row'); if (!row) return;
  const g = S.groups[Number(row.dataset.i)]; collectChar();
  if (e.target.classList.contains('g-on')) g.enabled = e.target.checked;
  else if (e.target.classList.contains('g-name')) {
    // 名称不合规就不改，重新画出来的还是原来的名称。
    const name = e.target.value.trim(), problem = cGroupProblem(name, g);
    if (problem) say(problem, 'err'); else g.name = name;
  }
  renderChars();
});
$('c-groups').onclick = e => {
  if (!e.target.classList.contains('g-del')) return;
  const g = S.groups[Number(e.target.closest('.named-row').dataset.i)], members = S.chars.filter(c => c.group_id === g.id);
  if (!confirm(`删除分组「${g.name}」？` + (members.length ? `里面的 ${members.length} 个角色会移到“${C_UNGROUPED}”。` : '') + '保存后生效。')) return;
  collectChar(); for (const c of members) c.group_id = ''; S.groups = S.groups.filter(x => x !== g); renderChars();
};
$('c-save').onclick = async () => {
  collectChar(); const problem = cProblem(); if (problem) return say(problem + '，改好后再保存', 'err');
  const keep = cCur() && cCur().id;
  try {
    cAdopt(await api('/characters', { groups: S.groups, characters: S.chars, revision: S.cRevision }), keep);
    $('c-note').textContent = ''; say('已保存', 'ok');
  } catch (e) { fail(e); }
};
$('c-reload').onclick = async () => {
  if (cDirty() && !confirm('放弃没保存的改动，重新载入角色库？')) return;
  const keep = cCur() && cCur().id;
  try { cAdopt(await api('/characters'), keep); $('c-note').textContent = ''; say('已重新载入', 'ok'); } catch (e) { fail(e); }
};
// 导入 / 导出：小白X 的角色标签文件。导入是并进当前草稿，保存后才生效；导出的是当前草稿。
$('c-import').onclick = () => $('c-file').click();
$('c-file').onchange = async () => {
  const file = $('c-file').files[0]; $('c-file').value = ''; if (!file) return;
  collectChar(); const problem = cProblem(); if (problem) return say(problem + '，改好后再导入', 'err');
  let data;
  try { data = JSON.parse(await file.text()); } catch (e) { return say('这个文件不是有效的 JSON', 'err'); }
  try {
    const keep = cCur() && cCur().id;
    const r = await api('/characters/import', { data, conflict: $('c-import-conflict').value, as_disabled: $('c-import-disabled').checked, groups: S.groups, characters: S.chars });
    S.groups = r.groups || []; S.chars = r.characters || []; S.cIndex = Math.max(0, S.chars.findIndex(c => c.id === keep)); renderChars();
    const s = r.summary || {};
    $('c-note').textContent = `已导入：新增 ${s.added || 0} 个，替换 ${s.replaced || 0} 个，跳过 ${s.skipped || 0} 个` + (s.new_groups ? `，新建分组 ${s.new_groups} 个` : '')
      + '。点“保存全部角色”后生效。' + ((r.notes || []).length ? '注意：' + r.notes.join('；') + '。' : '');
  } catch (e) { fail(e); }
};
$('c-export').onclick = async () => {
  collectChar(); const c = cCur(), scope = $('c-export-scope').value;
  const body = { groups: S.groups, characters: S.chars, scope: scope === 'one' ? 'selection' : scope };
  if (scope === 'group') body.group_id = c ? c.group_id || '' : '';
  if (scope === 'one') body.ids = c ? [c.id] : [];
  try { const r = await api('/characters/export', body); downloadJson(r.filename, r.data); say('已导出 ' + r.filename, 'ok'); } catch (e) { fail(e); }
};
window.addEventListener('beforeunload', e => { if (cDirty()) { e.preventDefault(); e.returnValue = ''; } });

/* ═══ 图片管理 ═══ */
const blobCache = new Map();
loaders.images = async () => {
  const d = await api('/images'); S.images = d.images || [];
  $('i-stats').innerHTML = `共 <b>${d.count}</b> 张，<b>${(d.total_bytes / 1048576).toFixed(1)}</b> MB`;
  $('i-grid').innerHTML = S.images.map(i => `<div class="thumb" data-name="${esc(i.name)}"><input type="checkbox"><img alt="" data-path="${esc(i.path)}"><div class="meta"><span title="${esc(i.name)}">${esc(i.name)}</span>${(i.size / 1024).toFixed(0)}K</div></div>`).join('');
  updateDelBtn();
  for (const img of $('i-grid').querySelectorAll('img')) {
    const p = img.dataset.path;
    try { if (!blobCache.has(p)) blobCache.set(p, await fetchImageBlob(p)); img.src = blobCache.get(p); } catch (e) {}
  }
};
function selectedImages() { return [...$('i-grid').querySelectorAll('.thumb')].filter(t => t.querySelector('input').checked).map(t => t.dataset.name); }
function updateDelBtn() { const n = selectedImages().length; $('i-del').disabled = !n; $('i-del').textContent = n ? `删除选中 (${n})` : '删除选中'; }
$('i-grid').addEventListener('change', e => { const t = e.target.closest('.thumb'); if (t) t.classList.toggle('sel', e.target.checked); updateDelBtn(); });
$('i-grid').addEventListener('click', e => { if (e.target.tagName === 'IMG' && e.target.src) { $('lightbox-img').src = e.target.src; $('lightbox').classList.add('show'); } });
$('lightbox').onclick = () => $('lightbox').classList.remove('show');
$('i-reload').onclick = () => loaders.images().catch(fail);
$('i-selall').onclick = () => { const boxes = [...$('i-grid').querySelectorAll('input')]; const all = boxes.every(b => b.checked); boxes.forEach(b => { b.checked = !all; b.closest('.thumb').classList.toggle('sel', !all); }); updateDelBtn(); };
$('i-del').onclick = async () => { const names = selectedImages(); if (!names.length || !confirm(`删除 ${names.length} 张图片？`)) return; try { const r = await api('/images/delete', { names }); say(`已删除 ${r.deleted.length} 张`, 'ok'); await loaders.images(); } catch (e) { fail(e); } };
$('i-cleanup').onclick = async () => { if (!confirm('按存储策略（保留天数 / 容量上限）清理旧图？')) return; try { const r = await api('/images/cleanup', {}); say(`已清理 ${r.deleted_count} 张`, 'ok'); await loaders.images(); } catch (e) { fail(e); } };

/* ═══ 快速测试 ═══ */
loaders.test = async () => {
  const d = await api('/presets');
  $('t-preset').innerHTML = `<option value="">当前默认</option>` + d.presets.map(p => `<option value="${esc(p.id)}">${esc(p.name || p.id)}</option>`).join('');
  if (!$('t-chars').children.length) addTestChar();
};
function addTestChar() {
  const row = document.createElement('div'); row.className = 'chars-row';
  row.innerHTML = `<input type="text" class="tc-name" placeholder="name"><input type="text" class="tc-costume" placeholder="costume"><input type="text" class="tc-action" placeholder="action, expression"><button class="op small danger tc-del">×</button>
    <input type="text" class="tc-appear" placeholder="appear（未录入角色必填）"><input type="text" class="tc-type" placeholder="type（可选，girl/boy/no_humans）"><input type="text" class="tc-center" placeholder="center 如 C3"><span></span>`;
  $('t-chars').appendChild(row);
}
$('t-add-char').onclick = addTestChar;
$('t-chars').onclick = e => { if (e.target.classList.contains('tc-del')) e.target.closest('.chars-row').remove(); };
function testBody(dry) {
  const chars = [...$('t-chars').querySelectorAll('.chars-row')].map(r => {
    const g = c => r.querySelector('.' + c).value.trim();
    const center = g('tc-center');
    const ch = { name: g('tc-name'), costume: g('tc-costume'), action: g('tc-action'), appear: g('tc-appear'), type: g('tc-type') };
    if (/^[A-E][1-5]$/i.test(center)) { const X = { A: .1, B: .3, C: .5, D: .7, E: .9 }, Y = { 1: .1, 2: .3, 3: .5, 4: .7, 5: .9 }; ch.center = { x: X[center[0].toUpperCase()], y: Y[center[1]] }; }
    return ch;
  }).filter(c => c.name);
  const seed = $('t-seed').value;
  return { scene: $('t-scene').value.trim(), orientation: $('t-orient').value, preset: $('t-preset').value || undefined, seed: seed === '' ? undefined : Number(seed),
           override_size: $('t-size').value || undefined, characters: chars, dry_run: !!dry };
}
async function runTest(dry) {
  const body = testBody(dry); if (!body.scene) return say('scene 不能为空', 'err');
  $('t-dry').disabled = $('t-run').disabled = true; $('t-state').textContent = dry ? '编译中…' : '生成中（可能需要 30~120 秒）…';
  $('t-result').style.display = ''; $('t-img').style.display = 'none';
  try {
    const r = await api('/test', body);
    const info = dry ? { prompt: r.prompt, negative_prompt: r.negative_prompt, character_prompts: r.character_prompts, payload: r.payload }
                     : { path: r.path, seed: r.seed, size: r.width + 'x' + r.height, prompt: r.prompt, negative_prompt: r.negative_prompt, character_prompts: r.character_prompts };
    $('t-out').textContent = JSON.stringify(info, null, 2);
    if (!dry) { $('t-img').src = await fetchImageBlob(r.path); $('t-img').style.display = ''; loaded.images = false; }
    $('t-state').textContent = dry ? '已编译（未发送请求）' : '完成'; say(dry ? '预览完成' : '生成完成', 'ok');
  } catch (e) { $('t-out').textContent = e.message + (e.details ? '\n' + JSON.stringify(e.details, null, 2) : ''); $('t-state').textContent = '失败'; fail(e); }
  finally { $('t-dry').disabled = $('t-run').disabled = false; }
}
$('t-dry').onclick = () => runTest(true);
$('t-run').onclick = () => runTest(false);

/* ═══ 提示词模板 ═══ */
// 开场 / TAG 指南 / 场景规则的文本在“请求结构”里对应的行上就地编辑，这里只保留 content 模板一个文本标签页。
const PR_TEXT_KEYS = ['content'];
loaders.prompts = async () => { S.prompts = await api('/prompts'); renderPromptTabs(); showPromptTab(S.prKey); };
function renderPromptTabs() {
  for (const b of $('pr-tabs').querySelectorAll('button')) {
    const k = b.dataset.key; b.classList.toggle('active', k === S.prKey);
    const dot = b.querySelector('.dot'); if (dot) dot.remove();
    if (S.prompts && S.prompts[k] && S.prompts[k].is_default === false) b.insertAdjacentHTML('beforeend', '<span class="dot" title="已自定义"></span>');
  }
}
async function showPromptTab(key) {
  S.prKey = key; renderPromptTabs();
  const isText = PR_TEXT_KEYS.includes(key);
  $('pr-editor').style.display = isText ? '' : 'none'; $('pr-fields').style.display = key === 'fields' ? '' : 'none';
  $('pr-layout').style.display = key === 'layout' ? '' : 'none'; $('pr-preview').style.display = key === 'preview' ? '' : 'none';
  if (isText) { const p = S.prompts[key]; $('pr-text').value = p.content; $('pr-state').textContent = p.is_default ? '当前为默认模板' : '已自定义'; }
  else if (key === 'fields') await loadFields().catch(fail);
  else if (key === 'layout') await loadLayout().catch(fail);
  else if (key === 'preview') await loadPreview().catch(fail);
}
$('pr-tabs').onclick = e => { const b = e.target.closest('button[data-key]'); if (b) showPromptTab(b.dataset.key); };
$('pr-save').onclick = async () => { try { S.prompts[S.prKey] = await api('/prompts/' + S.prKey, { content: $('pr-text').value }); showPromptTab(S.prKey); say('已保存', 'ok'); } catch (e) { fail(e); } };
$('pr-reset').onclick = async () => { if (!confirm('恢复此模板为默认？自定义内容将被删除。')) return; try { S.prompts[S.prKey] = await api('/prompts/' + S.prKey + '/reset', {}); showPromptTab(S.prKey); say('已恢复默认', 'ok'); } catch (e) { fail(e); } };

/* 工具字段说明 */
async function loadFields() {
  S.fields = await api('/fields');
  const ov = S.fields.overrides || {};
  $('pr-fields-list').innerHTML = S.fields.fields.map(f => `<label class="f wide" style="margin-bottom:8px"><span>${esc(f.path)}${ov[f.path] !== undefined ? ' <b style="color:var(--warn)">已自定义</b>' : ''}</span><textarea data-path="${esc(f.path)}" style="min-height:44px">${esc(f.description)}</textarea><span class="hint">默认：${esc(S.fields.defaults[f.path] || '')}</span></label>`).join('');
}
$('pr-fields-save').onclick = async () => {
  const out = {};
  for (const ta of $('pr-fields-list').querySelectorAll('textarea')) { const p = ta.dataset.path, v = ta.value.trim(); if (v && v !== (S.fields.defaults[p] || '')) out[p] = v; }
  try { await api('/settings', { planner: { field_descriptions: out } }); await loadFields(); say('已保存', 'ok'); } catch (e) { fail(e); }
};
$('pr-fields-reset').onclick = async () => { if (!confirm('所有字段说明恢复默认？')) return; try { await api('/settings', { planner: { field_descriptions: {} } }); await loadFields(); say('已恢复默认', 'ok'); } catch (e) { fail(e); } };

/* 请求结构（编辑器由 layout-editor.js 提供 window.DrawLayoutEditor；缺失时降级为 JSON 文本） */
let layoutEditor = null;
// 编辑器里内置块的就地编辑与上面几个文本标签页读写同一份 S.prompts。
// “TAG指南”行对应当前默认预设实际用的那份（V4.5 是 guide，V5 模型是 guide_v5），由后端的 guide_key 决定。
const promptKey = key => (key === 'guide' && S.prompts && S.prompts.guide_key) ? S.prompts.guide_key : key;
const layoutPrompts = {
  get: key => (S.prompts ? S.prompts[promptKey(key)] : undefined),
  save: async (key, content) => { S.prompts[promptKey(key)] = await api('/prompts/' + promptKey(key), { content }); renderPromptTabs(); },
  reset: async key => { S.prompts[promptKey(key)] = await api('/prompts/' + promptKey(key) + '/reset', {}); renderPromptTabs(); },
};
function layoutStateText() {
  const notes = (S.layout && S.layout.notes) || [];
  return (S.layout.is_default ? '已保存的是默认结构' : '已保存的是自定义结构') + (notes.length ? '（已自动修正：' + notes.join('；') + '）' : '');
}
async function loadLayout(force) {
  // 结构草稿有未保存的改动时不重新拉取：切到别的标签页再回来不能把草稿冲掉。
  if (layoutEditor && !force && layoutEditor.isDirty()) { layoutEditor.refreshBuiltinTexts(); return; }
  S.layout = await api('/layout');
  $('pr-layout-state').textContent = layoutStateText();
  const root = $('layout-editor-root');
  if (window.DrawLayoutEditor) {
    if (!layoutEditor) layoutEditor = window.DrawLayoutEditor.mount(root, { api, say, prompts: layoutPrompts });
    layoutEditor.setLayout(S.layout.layout);
  } else {
    root.innerHTML = '<textarea class="tall" id="layout-json"></textarea><div class="hint">结构编辑器未加载，以 JSON 直接编辑。</div>';
    $('layout-json').value = JSON.stringify(S.layout.layout, null, 2);
  }
}
function currentLayout() {
  if (layoutEditor) return layoutEditor.getLayout();
  return JSON.parse($('layout-json').value);
}
$('pr-layout-save').onclick = async () => { try { await api('/layout', { layout: currentLayout() }); await loadLayout(true); say('结构已保存', 'ok'); } catch (e) { fail(e); } };
$('pr-layout-discard').onclick = async () => {
  if (layoutEditor && layoutEditor.isDirty() && !confirm('放弃请求结构里未保存的改动，回到已保存的结构？')) return;
  try { await loadLayout(true); say('已回到已保存的结构', 'ok'); } catch (e) { fail(e); }
};
// 结构草稿或内置块文本没保存就关页面时提醒一次。
window.addEventListener('beforeunload', e => { if (layoutEditor && (layoutEditor.isDirty() || layoutEditor.hasUnsavedText())) { e.preventDefault(); e.returnValue = ''; } });

/* 链预览 */
async function loadPreview() {
  const body = {};
  // 请求结构里有未保存的草稿（结构或内置块文本）时，预览按草稿组装，并在旁边注明。
  const draft = !!layoutEditor && (layoutEditor.isDirty() || layoutEditor.hasUnsavedText());
  if (draft) { body.layout = layoutEditor.getLayout(); body.texts = layoutEditor.getDraftTexts(); }
  $('pr-preview-note').textContent = draft ? '当前按“请求结构”里尚未保存的草稿组装。' : '';
  const chain = (await api('/preview', body)).chain;
  $('pr-chain').innerHTML = chain.map((m, i) => {
    const body = m.role === 'tools'
      ? `<div class="sec"><div class="k">字段说明</div><pre class="code">${esc((m.fields || []).map(f => f.path + ': ' + f.description).join('\n'))}</pre></div><div class="sec"><div class="k">工具 schema</div><pre class="code">${esc(JSON.stringify(m.tool, null, 2))}</pre></div>`
      : (m.sections || []).map(s => `<div class="sec"><div class="k">${esc(s.summary)}${s.builtin ? '' : ' · 自定义'}</div><pre class="code">${esc(s.content || '（空）')}</pre></div>`).join('');
    return `<div class="msg" data-i="${i}"><div class="bar"><span class="role ${esc(m.role)}">${esc(m.roleLabel || m.role)}</span><span class="sum">${esc(m.summary)}</span>${m.stageId ? `<span class="hint">${esc(m.stageId)}</span>` : ''}</div><div class="body">${body}</div></div>`;
  }).join('');
}
$('pr-chain').onclick = e => { const bar = e.target.closest('.bar'); if (bar) bar.parentElement.classList.toggle('open'); };
$('pr-preview-reload').onclick = () => loadPreview().catch(fail);

/* ── 启动 ── */
const initial = (location.hash || '#test').slice(1);
show(document.querySelector(`nav.side a[data-view="${initial}"]`) ? initial : 'test');
})();
