// Model settings: channel schemes (type + address + key + model), the current scheme,
// system model slots and the fallback chain. Every model user picks a scheme by id.
import { useEffect, useState } from 'react';

import {
  type ChannelPublic,
  type ChannelRef,
  type ChannelsResponse,
  deleteChannel,
  getChannels,
  pullChannelModels,
  reloadConfig,
  setActiveChannel,
  setModelVision,
  updateFallbacks,
  updateSystemModel,
  upsertChannel,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { errorText, notify } from '../../../store/toastStore';
import { Button } from '../../common';
import { SystemModelCard } from './SystemModelCard';

interface ChannelEdit {
  name: string;
  type: string;
  base_url: string;
  model: string;
  api_key: string;
}

interface FallbackRow {
  key: number;
  channel: string;
  model: string;
  source_index: number | null;
}

const inputCls =
  'w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]';
const labelCls = 'mb-1 block text-xs text-[var(--duties-secondary)]';

let rowSeq = 0;

const editFrom = (c: ChannelPublic): ChannelEdit => ({
  name: c.name, type: c.type, base_url: c.base_url, model: c.model, api_key: '',
});

const SYSTEM_SLOTS = [
  { slot: 'compact', title: '上下文压缩模型', description: '会话超过压缩阈值时生成摘要。建议用便宜、上下文长的模型。' },
  { slot: 'summary', title: '轮次摘要模型', description: '为每轮任务生成简短执行摘要。' },
  { slot: 'memory', title: '记忆提取模型', description: '从对话中提取值得长期保存的信息（做梦整理时使用）。' },
  { slot: 'vision', title: '识图模型', description: 'read_media 工具和“不可识图模型收到图片时的自动转写”使用；需要 Gemini 类型的方案。' },
  { slot: 'image', title: '生图(Gemini)', description: 'gemini_image 工具使用；需要 Gemini 类型的方案。不设置则该工具不可用。' },
  { slot: 'image_gpt', title: '生图(GPT)', description: 'gpt_image_2 工具使用；OpenAI 格式的图片接口。不设置则该工具不可用。' },
];
const UNSET_MEANS_OFF = new Set(['image', 'image_gpt']);

export const ModelSettingsPage = () => {
  const { adminToken, isAuthenticated, setChannelConfig } = useSettingsStore();

  const [data, setData] = useState<ChannelsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [edits, setEdits] = useState<Record<string, ChannelEdit>>({});
  const [models, setModels] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const [newId, setNewId] = useState('');
  const [newType, setNewType] = useState('');

  const [fbRows, setFbRows] = useState<FallbackRow[]>([]);
  const [fbDirty, setFbDirty] = useState(false);

  const apply = (resp: ChannelsResponse) => {
    setData(resp);
    setEdits(Object.fromEntries(resp.channels.map(c => [c.id, editFrom(c)])));
    setFbRows(resp.fallbacks.map((f, i) => ({ key: ++rowSeq, channel: f.channel, model: f.model, source_index: i })));
    setFbDirty(false);
    setChannelConfig(resp.active_channel, resp.channels.map(c => ({ id: c.id, name: c.name, type: c.type, model: c.model })));
  };

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    try {
      apply(await getChannels(adminToken));
    } catch (err) {
      notify.error(errorText(err, '加载失败'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [adminToken, isAuthenticated]);

  const run = async (key: string, fn: () => Promise<ChannelsResponse | void>, done?: string) => {
    if (!adminToken) return;
    setBusy(key);
    try {
      const resp = await fn();
      if (resp) apply(resp);
      if (done) notify.success(done);
    } catch (err) {
      notify.error(errorText(err, '操作失败'));
    } finally {
      setBusy(null);
    }
  };

  const updateEdit = (id: string, patch: Partial<ChannelEdit>) =>
    setEdits(prev => ({ ...prev, [id]: { ...prev[id], ...patch } }));

  const saveChannel = (c: ChannelPublic) => {
    const e = edits[c.id];
    return run(`save:${c.id}`, () => upsertChannel(adminToken!, c.id, {
      name: e.name, type: e.type, base_url: e.base_url, model: e.model, ...(e.api_key ? { api_key: e.api_key } : {}),
    }), `方案「${e.name || c.id}」已保存`);
  };

  const pull = (c: ChannelPublic) => {
    const e = edits[c.id];
    return run(`pull:${c.id}`, async () => {
      const out = await pullChannelModels(adminToken!, { channel: c.id, type: e.type, base_url: e.base_url, api_key: e.api_key });
      setModels(prev => ({ ...prev, [c.id]: out.models }));
      notify.success(`拉取到 ${out.models.length} 个模型`);
    });
  };

  const addChannel = () => {
    const id = newId.trim();
    if (!id || !newType) return;
    void run('add', () => upsertChannel(adminToken!, id, { name: id, type: newType }), `方案「${id}」已添加`).then(() => {
      setNewId('');
      setNewType('');
    });
  };

  const saveRef = async (slot: string, ref: ChannelRef) => {
    await updateSystemModel(adminToken!, slot, ref);
    apply(await getChannels(adminToken!));
  };

  const editRows = (fn: (rows: FallbackRow[]) => FallbackRow[]) => {
    setFbRows(fn);
    setFbDirty(true);
  };

  const saveFallbacks = () => {
    if (fbRows.some(r => !r.channel)) {
      notify.error('每个备用项都需要选择渠道方案');
      return;
    }
    void run('fallbacks', () => updateFallbacks(adminToken!, fbRows.map(r => ({
      channel: r.channel, model: r.model.trim(), source_index: r.source_index,
    }))), '备用链已保存');
  };

  const channels = data?.channels || [];
  const active = data?.active_channel || '';

  return (
    <section className="mx-auto flex h-full max-w-4xl flex-col gap-5 overflow-y-auto p-4 sm:p-6">
      <div>
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">设置</p>
        <h2 className="mt-1 font-mono text-xl font-semibold tracking-[-0.04em]">模型与渠道方案</h2>
        <p className="mt-2 max-w-2xl text-sm text-[var(--duties-secondary)]">
          一个渠道方案 = 接口类型 + 地址 + 密钥 + 默认模型。对话默认用“当前方案”；系统模型、节点、备用链、绘图场景 Agent 都从这里选方案。
        </p>
      </div>

      {!isAuthenticated ? (
        <div className="border border-[var(--duties-border)] p-4 text-sm text-[var(--duties-secondary)]">修改模型设置前需要完成管理员令牌认证。</div>
      ) : loading && !data ? (
        <div className="p-4 text-sm text-[var(--duties-secondary)]">加载中...</div>
      ) : (
        <>
          {/* Current scheme */}
          <div className="border border-[var(--duties-text)] bg-[var(--duties-panel)] p-4">
            <p className="mb-2 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">当前方案</p>
            {channels.length === 0 ? (
              <p className="text-xs text-[var(--duties-secondary)]">还没有渠道方案，先在下方添加一个。</p>
            ) : (
              <select
                className={inputCls}
                disabled={busy === 'active'}
                onChange={e => void run('active', () => setActiveChannel(adminToken!, e.target.value), '已切换当前方案')}
                value={active}
              >
                {channels.map(c => (
                  <option key={c.id} value={c.id}>{c.name}（{c.type}）{c.model ? ` — ${c.model}` : ''}</option>
                ))}
              </select>
            )}
          </div>

          {/* Scheme cards */}
          {channels.map(c => {
            const e = edits[c.id] || editFrom(c);
            const isActive = c.id === active;
            const pulled = models[c.id] || [];
            return (
              <div key={c.id} className={`border bg-[var(--duties-panel)] p-4 ${isActive ? 'border-[var(--duties-text)]' : 'border-[var(--duties-border)]'}`}>
                <div className="mb-3 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <p className="font-mono text-sm font-semibold text-[var(--duties-text)]">{c.name}</p>
                    <span className="font-mono text-[0.6rem] text-[var(--duties-tertiary)]">{c.id}</span>
                    {isActive && (
                      <span className="rounded bg-[var(--duties-text)] px-1.5 py-0.5 text-[0.6rem] font-bold text-[var(--duties-bg)]">当前</span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 text-xs">
                    {!isActive && (
                      <button className="text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)]"
                        onClick={() => void run('active', () => setActiveChannel(adminToken!, c.id), `已切换到「${c.name}」`)}>
                        设为当前
                      </button>
                    )}
                    {!isActive && (
                      <button className="text-red-400 underline hover:text-red-300"
                        onClick={() => window.confirm(`确定删除方案「${c.name}」？`) && void run(`del:${c.id}`, () => deleteChannel(adminToken!, c.id), '已删除')}>
                        删除
                      </button>
                    )}
                  </div>
                </div>
                <div className="grid gap-2 sm:grid-cols-2">
                  <div>
                    <label className={labelCls}>名称</label>
                    <input className={inputCls} onChange={ev => updateEdit(c.id, { name: ev.target.value })} value={e.name} />
                  </div>
                  <div>
                    <label className={labelCls}>接口类型</label>
                    <select className={inputCls} onChange={ev => updateEdit(c.id, { type: ev.target.value })} value={e.type}>
                      {Array.from(new Set([e.type, ...(data?.registered || [])])).filter(Boolean).map(t => (
                        <option key={t} value={t}>{t}</option>
                      ))}
                    </select>
                  </div>
                  <div className="sm:col-span-2">
                    <label className={labelCls}>
                      基础 URL {c.base_url !== c.base_url_resolved && c.base_url_resolved ? `（实际：${c.base_url_resolved}）` : ''}
                    </label>
                    <input className={inputCls} onChange={ev => updateEdit(c.id, { base_url: ev.target.value })}
                      placeholder="留空使用该类型的官方地址；可写 ${ENV_NAME}" value={e.base_url} />
                    {c.type_hint && e.type === c.type && e.base_url === c.base_url && (
                      <p className="mt-1 text-xs text-yellow-500">{c.type_hint}</p>
                    )}
                  </div>
                  <div>
                    <label className={labelCls}>API 密钥 {c.api_key_present ? `（已设置: ${c.api_key_redacted}）` : '（未设置）'}</label>
                    <input className={inputCls} onChange={ev => updateEdit(c.id, { api_key: ev.target.value })}
                      placeholder="留空以保留当前值" type="password" value={e.api_key} />
                  </div>
                  <div>
                    <label className={labelCls}>默认模型</label>
                    <div className="flex gap-2">
                      {pulled.length > 0 ? (
                        <select className={inputCls} onChange={ev => updateEdit(c.id, { model: ev.target.value })} value={e.model}>
                          {!pulled.includes(e.model) && <option value={e.model}>{e.model || '（未选择）'}</option>}
                          {pulled.map(m => <option key={m} value={m}>{m}</option>)}
                        </select>
                      ) : (
                        <input className={inputCls} onChange={ev => updateEdit(c.id, { model: ev.target.value })} value={e.model} />
                      )}
                      <Button disabled={busy === `pull:${c.id}`} onClick={() => void pull(c)}>
                        {busy === `pull:${c.id}` ? '拉取中' : '拉取模型'}
                      </Button>
                    </div>
                  </div>
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-4">
                  <Button disabled={busy === `save:${c.id}`} onClick={() => void saveChannel(c)} variant="primary">
                    {busy === `save:${c.id}` ? '保存中...' : '保存'}
                  </Button>
                  {c.api_key_present && (
                    <button className="text-xs text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)]"
                      onClick={() => void run(`save:${c.id}`, () => upsertChannel(adminToken!, c.id, { clear_api_key: true }), '密钥已清除')}>
                      清除密钥
                    </button>
                  )}
                  <label className="flex items-center gap-2 text-xs text-[var(--duties-secondary)]">
                    <input checked={c.vision} disabled={!c.model}
                      onChange={ev => void run('vision', () => setModelVision(adminToken!, c.model, ev.target.checked))} type="checkbox" />
                    {c.model || '模型'} 可识图
                    <span className="text-[var(--duties-tertiary)]">{c.vision ? '图片直接发给模型' : '图片先转写成文字'}</span>
                  </label>
                </div>
              </div>
            );
          })}

          {/* Add scheme */}
          <div className="border border-dashed border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
            <p className="mb-3 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">添加渠道方案</p>
            <div className="flex flex-wrap items-center gap-3">
              <input className={`${inputCls} flex-1`} onChange={e => setNewId(e.target.value)} placeholder="方案 id（字母数字 _ . -）" value={newId} />
              <select className={`${inputCls} flex-1`} onChange={e => setNewType(e.target.value)} value={newType}>
                <option value="">选择接口类型...</option>
                {(data?.registered || []).map(t => <option key={t} value={t}>{t}</option>)}
              </select>
              <Button disabled={busy === 'add' || !newId.trim() || !newType} onClick={addChannel} variant="primary">添加</Button>
            </div>
          </div>

          {/* System slots */}
          {data && SYSTEM_SLOTS.map(s => (
            <SystemModelCard
              key={s.slot}
              activeChannel={active}
              channels={channels}
              description={s.description}
              emptyLabel={UNSET_MEANS_OFF.has(s.slot) && !data.system_models[s.slot]?.model ? '不使用（工具不可用）' : undefined}
              onSave={ref => saveRef(s.slot, ref)}
              title={s.title}
              value={data.system_models[s.slot]}
            />
          ))}

          {/* Fallback chain */}
          <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
            <p className="mb-1 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">备用链</p>
            <p className="mb-3 text-xs text-[var(--duties-secondary)]">主请求失败时按顺序尝试。每项选一个方案，模型留空用方案的默认模型。</p>
            {fbRows.length === 0 && <p className="mb-3 text-xs text-[var(--duties-tertiary)]">未配置备用项。</p>}
            {fbRows.map((row, i) => {
              const ch = channels.find(c => c.id === row.channel);
              return (
                <div key={row.key} className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="w-6 font-mono text-xs text-[var(--duties-tertiary)]">#{i + 1}</span>
                  <select className={`${inputCls} flex-1`} onChange={e => editRows(rows => rows.map(r => r.key === row.key ? { ...r, channel: e.target.value } : r))} value={row.channel}>
                    <option value="">选择方案...</option>
                    {channels.map(c => <option key={c.id} value={c.id}>{c.name}（{c.type}）</option>)}
                    {row.channel && !ch && <option value={row.channel}>{row.channel}（不存在）</option>}
                  </select>
                  <input className={`${inputCls} flex-1`} onChange={e => editRows(rows => rows.map(r => r.key === row.key ? { ...r, model: e.target.value } : r))}
                    placeholder={ch?.model ? `留空使用 ${ch.model}` : '模型'} value={row.model} />
                  <div className="flex gap-2 text-xs">
                    <button className="underline disabled:opacity-40" disabled={i === 0}
                      onClick={() => editRows(rows => { const n = [...rows]; [n[i - 1], n[i]] = [n[i], n[i - 1]]; return n; })}>上移</button>
                    <button className="underline disabled:opacity-40" disabled={i === fbRows.length - 1}
                      onClick={() => editRows(rows => { const n = [...rows]; [n[i + 1], n[i]] = [n[i], n[i + 1]]; return n; })}>下移</button>
                    <button className="text-red-400 underline" onClick={() => editRows(rows => rows.filter(r => r.key !== row.key))}>删除</button>
                  </div>
                </div>
              );
            })}
            <div className="mt-3 flex items-center gap-3">
              <Button onClick={() => editRows(rows => [...rows, { key: ++rowSeq, channel: active, model: '', source_index: null }])}>添加备用项</Button>
              <Button disabled={busy === 'fallbacks' || !fbDirty} onClick={saveFallbacks} variant="primary">保存备用链</Button>
              {fbDirty && <span className="text-xs text-[var(--duties-tertiary)]">有未保存的修改</span>}
            </div>
          </div>

          <div className="flex items-center gap-3">
            <Button onClick={() => void run('reload', async () => { await reloadConfig(adminToken!); return getChannels(adminToken!); }, '配置已重载')}>🔄 重载配置</Button>
            <p className="text-xs text-[var(--duties-secondary)]">重新读取 config.yaml，不中断服务</p>
          </div>
        </>
      )}
    </section>
  );
};
