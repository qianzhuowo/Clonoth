// [2026-06-03] Model & Provider settings — dynamic multi-provider editor.
import { useEffect, useState } from 'react';

import {
  type FallbackEntryUpdate,
  type ProviderConfigPublic,
  type ProvidersResponse,
  type SystemModelPublic,
  deleteProvider,
  getProviders,
  getSystemModel,
  reloadConfig,
  setActiveProvider,
  setModelVision,
  updateFallbacks,
  updateSystemModel,
  upsertProvider,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';

interface ProviderEditState {
  base_url: string;
  model: string;
  api_key: string;
}

interface FallbackRow extends FallbackEntryUpdate {
  key: number;
  vision: boolean;
  api_key_present: boolean;
  api_key_redacted: string;
}

const emptyEdit = (): ProviderEditState => ({ base_url: '', model: '', api_key: '' });

const inputCls =
  'w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]';

let rowSeq = 0;

const rowsFrom = (resp: ProvidersResponse): FallbackRow[] =>
  resp.fallbacks.map((fb, i) => ({
    key: ++rowSeq,
    provider: fb.provider,
    model: fb.model,
    base_url: fb.base_url,
    api_key: '',
    clear_api_key: false,
    source_index: i,
    vision: fb.vision,
    api_key_present: fb.api_key_present,
    api_key_redacted: fb.api_key_redacted,
  }));

const modelVision = (resp: ProvidersResponse | null, model: string): boolean => {
  const flag = resp?.model_capabilities?.[model.trim()]?.vision;
  return flag === undefined ? true : Boolean(flag);
};

export const ModelSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();

  const [data, setData] = useState<ProvidersResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');

  // per-provider edit states, keyed by provider name
  const [edits, setEdits] = useState<Record<string, ProviderEditState>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);

  // new provider: select from registered list
  const [newName, setNewName] = useState('');
  const [addingNew, setAddingNew] = useState(false);

  // fallback chain editor
  const [fbRows, setFbRows] = useState<FallbackRow[]>([]);
  const [fbDirty, setFbDirty] = useState(false);
  const [fbSaving, setFbSaving] = useState(false);

  // vision model (system_models.vision)
  const [visionSlot, setVisionSlot] = useState<SystemModelPublic | null>(null);
  const [visionEdit, setVisionEdit] = useState<ProviderEditState>(emptyEdit());
  const [visionSaving, setVisionSaving] = useState(false);

  const applyResponse = (resp: ProvidersResponse) => {
    setData(resp);
    const e: Record<string, ProviderEditState> = {};
    for (const [name, cfg] of Object.entries(resp.providers)) {
      e[name] = { base_url: cfg.base_url, model: cfg.model, api_key: '' };
    }
    setEdits(e);
    setFbRows(rowsFrom(resp));
    setFbDirty(false);
  };

  const applyVisionSlot = (slot: SystemModelPublic) => {
    setVisionSlot(slot);
    setVisionEdit({ model: slot.model, base_url: slot.base_url, api_key: '' });
  };

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    setMsg('');
    try {
      const [resp, slot] = await Promise.all([getProviders(adminToken), getSystemModel(adminToken, 'vision')]);
      applyResponse(resp);
      applyVisionSlot(slot);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [adminToken, isAuthenticated]);

  const handleSave = async (name: string) => {
    if (!adminToken) return;
    const edit = edits[name];
    if (!edit) return;
    setSavingKey(name);
    setMsg('');
    try {
      const params: Record<string, string> = {};
      if (edit.base_url) params.base_url = edit.base_url;
      if (edit.model) params.model = edit.model;
      if (edit.api_key) params.api_key = edit.api_key;
      const resp = await upsertProvider(adminToken, name, params);
      applyResponse(resp);
      setMsg(`${name} 已保存`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '保存失败');
    } finally {
      setSavingKey(null);
    }
  };

  const handleSetActive = async (name: string) => {
    if (!adminToken) return;
    setMsg('');
    try {
      const resp = await setActiveProvider(adminToken, name);
      applyResponse(resp);
      setMsg(`已切换到 ${name}`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '切换失败');
    }
  };

  const handleDelete = async (name: string) => {
    if (!adminToken) return;
    if (!window.confirm(`确定要删除渠道 "${name}"？`)) return;
    setMsg('');
    try {
      const resp = await deleteProvider(adminToken, name);
      applyResponse(resp);
      setMsg(`${name} 已删除`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '删除失败');
    }
  };

  const handleAddProvider = async () => {
    if (!adminToken || !newName) return;
    const name = newName;
    setAddingNew(true);
    setMsg('');
    try {
      const resp = await upsertProvider(adminToken, name, {});
      applyResponse(resp);
      setNewName('');
      setMsg(`渠道 ${name} 已添加`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '添加失败');
    } finally {
      setAddingNew(false);
    }
  };

  const handleReload = async () => {
    if (!adminToken) return;
    setMsg('');
    try {
      await reloadConfig(adminToken);
      await load();
      setMsg('✅ 配置已重载');
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '重载失败');
    }
  };

  const updateEdit = (name: string, field: keyof ProviderEditState, value: string) => {
    setEdits(prev => ({ ...prev, [name]: { ...prev[name], [field]: value } }));
  };

  const handleToggleVision = async (model: string, vision: boolean) => {
    if (!adminToken || !model.trim()) return;
    setMsg('');
    try {
      applyResponse(await setModelVision(adminToken, model.trim(), vision));
      setMsg(`${model} 已标记为${vision ? '可识图' : '不可识图'}`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '保存失败');
    }
  };

  const handleSaveVisionSlot = async () => {
    if (!adminToken) return;
    setVisionSaving(true);
    setMsg('');
    try {
      const payload: { model: string; base_url: string; api_key?: string } = {
        model: visionEdit.model,
        base_url: visionEdit.base_url,
      };
      if (visionEdit.api_key) payload.api_key = visionEdit.api_key;
      applyVisionSlot(await updateSystemModel(adminToken, 'vision', payload));
      setMsg('识图模型已保存');
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '保存失败');
    } finally {
      setVisionSaving(false);
    }
  };

  const handleClearVisionKey = async () => {
    if (!adminToken) return;
    try {
      applyVisionSlot(await updateSystemModel(adminToken, 'vision', { api_key: '' }));
      setMsg('识图模型密钥已清除，将使用环境变量中的密钥');
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '保存失败');
    }
  };

  const updateRow = (key: number, patch: Partial<FallbackRow>) => {
    setFbRows(rows => rows.map(r => (r.key === key ? { ...r, ...patch } : r)));
    setFbDirty(true);
  };

  const moveRow = (index: number, delta: number) => {
    setFbRows(rows => {
      const target = index + delta;
      if (target < 0 || target >= rows.length) return rows;
      const next = [...rows];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
    setFbDirty(true);
  };

  const addRow = () => {
    const provider = data?.active_provider || data?.registered?.[0] || 'openai';
    setFbRows(rows => [
      ...rows,
      {
        key: ++rowSeq,
        provider,
        model: '',
        base_url: '',
        api_key: '',
        clear_api_key: false,
        source_index: null,
        vision: true,
        api_key_present: false,
        api_key_redacted: '',
      },
    ]);
    setFbDirty(true);
  };

  const removeRow = (key: number) => {
    setFbRows(rows => rows.filter(r => r.key !== key));
    setFbDirty(true);
  };

  const handleSaveFallbacks = async () => {
    if (!adminToken) return;
    if (fbRows.some(r => !r.provider.trim() || !r.model.trim())) {
      setMsg('每个备用模型都需要填写渠道类型和模型名');
      return;
    }
    setFbSaving(true);
    setMsg('');
    try {
      let resp = await updateFallbacks(
        adminToken,
        fbRows.map(r => ({
          provider: r.provider,
          model: r.model,
          base_url: r.base_url,
          api_key: r.api_key,
          clear_api_key: r.clear_api_key,
          source_index: r.source_index,
        })),
      );
      // Vision is a property of the model name (model_capabilities), shared with provider cards.
      const seen = new Set<string>();
      for (const r of fbRows) {
        const model = r.model.trim();
        if (seen.has(model)) continue;
        seen.add(model);
        if (modelVision(resp, model) !== r.vision) {
          resp = await setModelVision(adminToken, model, r.vision);
        }
      }
      applyResponse(resp);
      setMsg('备用模型链已保存');
    } catch (err) {
      setMsg(err instanceof Error ? err.message : '保存失败');
    } finally {
      setFbSaving(false);
    }
  };

  const providerNames = data ? Object.keys(data.providers) : [];

  return (
    <section className="mx-auto flex h-full max-w-4xl flex-col gap-5 overflow-y-auto p-4 sm:p-6">
      <div>
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">设置</p>
        <h2 className="mt-1 font-mono text-xl font-semibold tracking-[-0.04em]">模型与渠道</h2>
        <p className="mt-2 max-w-2xl text-sm text-[var(--duties-secondary)]">
          管理 LLM Provider 渠道配置。当前活跃渠道会被标记。
        </p>
      </div>

      {!isAuthenticated ? (
        <div className="border border-[var(--duties-border)] p-4 text-sm text-[var(--duties-secondary)]">
          修改模型设置前需要完成管理员令牌认证。
        </div>
      ) : loading && !data ? (
        <div className="p-4 text-sm text-[var(--duties-secondary)]">加载中...</div>
      ) : (
        <>
          {/* Status bar */}
          {msg && (
            <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] px-4 py-2 text-xs text-[var(--duties-tertiary)]">
              {msg}
            </div>
          )}

          {/* Provider cards */}
          {providerNames.map(name => {
            const cfg = data!.providers[name];
            const edit = edits[name] || emptyEdit();
            const isActive = data!.active_provider === name;
            const isSaving = savingKey === name;

            return (
              <div
                key={name}
                className={`border bg-[var(--duties-panel)] p-4 ${
                  isActive
                    ? 'border-[var(--duties-text)]'
                    : 'border-[var(--duties-border)]'
                }`}
              >
                <div className="mb-3 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <p className="font-mono text-sm font-semibold text-[var(--duties-text)]">{name}</p>
                    {isActive && (
                      <span className="rounded bg-[var(--duties-text)] px-1.5 py-0.5 text-[0.6rem] font-bold text-[var(--duties-bg)]">
                        ACTIVE
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    {!isActive && (
                      <button
                        className="text-xs text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)]"
                        onClick={() => handleSetActive(name)}
                      >
                        设为活跃
                      </button>
                    )}
                    {!isActive && (
                      <button
                        className="text-xs text-red-400 underline hover:text-red-300"
                        onClick={() => handleDelete(name)}
                      >
                        删除
                      </button>
                    )}
                  </div>
                </div>

                <label className="mb-1 block text-xs text-[var(--duties-secondary)]">模型</label>
                <input
                  className="mb-3 w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                  onChange={e => updateEdit(name, 'model', e.target.value)}
                  value={edit.model}
                />
                <label className="mb-1 block text-xs text-[var(--duties-secondary)]">基础 URL</label>
                <input
                  className="mb-3 w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                  onChange={e => updateEdit(name, 'base_url', e.target.value)}
                  value={edit.base_url}
                />
                <label className="mb-1 block text-xs text-[var(--duties-secondary)]">
                  API 密钥 {cfg?.api_key_present ? `（已设置: ${cfg.api_key_redacted}）` : '（未设置）'}
                </label>
                <input
                  className="mb-3 w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                  onChange={e => updateEdit(name, 'api_key', e.target.value)}
                  placeholder="留空以保留当前值"
                  type="password"
                  value={edit.api_key}
                />
                <label className="mb-3 flex items-center gap-2 text-xs text-[var(--duties-secondary)]">
                  <input
                    checked={cfg?.vision ?? true}
                    disabled={!cfg?.model}
                    onChange={e => handleToggleVision(cfg.model, e.target.checked)}
                    type="checkbox"
                  />
                  可识图（vision）
                  <span className="text-[var(--duties-tertiary)]">
                    {cfg?.vision ?? true ? '图片直接发给该模型' : '图片先交给识图模型转写成文字'}
                  </span>
                </label>
                <Button disabled={isSaving} onClick={() => handleSave(name)} variant="primary">
                  {isSaving ? '保存中...' : '保存'}
                </Button>
              </div>
            );
          })}

          {/* Add new provider from registered list */}
          {(() => {
            const available = (data?.registered || []).filter(r => !providerNames.includes(r));
            if (available.length === 0) return null;
            return (
              <div className="border border-dashed border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
                <p className="mb-3 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">添加渠道</p>
                <div className="flex items-center gap-3">
                  <select
                    className="flex-1 border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
                    onChange={e => setNewName(e.target.value)}
                    value={newName}
                  >
                    <option value="">选择渠道类型...</option>
                    {available.map(name => (
                      <option key={name} value={name}>{name}</option>
                    ))}
                  </select>
                  <Button disabled={addingNew || !newName} onClick={handleAddProvider} variant="primary">
                    {addingNew ? '添加中...' : '添加'}
                  </Button>
                </div>
              </div>
            );
          })()}

          {/* Vision model (system_models.vision) */}
          <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
            <p className="mb-1 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">识图模型</p>
            <p className="mb-3 text-xs text-[var(--duties-secondary)]">
              read_media 工具和“不可识图模型收到图片时的自动转写”使用。走 Gemini 原生接口（generateContent）；
              留空的字段使用 CLONOTH_VISION_* / GEMINI_* / OPENAI_* 环境变量。
            </p>
            <label className="mb-1 block text-xs text-[var(--duties-secondary)]">模型</label>
            <input
              className={`mb-3 ${inputCls}`}
              onChange={e => setVisionEdit(v => ({ ...v, model: e.target.value }))}
              placeholder="留空默认 gemini-3.8-flash"
              value={visionEdit.model}
            />
            <label className="mb-1 block text-xs text-[var(--duties-secondary)]">基础 URL</label>
            <input
              className={`mb-3 ${inputCls}`}
              onChange={e => setVisionEdit(v => ({ ...v, base_url: e.target.value }))}
              placeholder="留空使用环境变量"
              value={visionEdit.base_url}
            />
            <label className="mb-1 block text-xs text-[var(--duties-secondary)]">
              API 密钥 {visionSlot?.api_key_present ? `（已设置: ${visionSlot.api_key_redacted}）` : '（未设置，使用环境变量）'}
            </label>
            <input
              className={`mb-3 ${inputCls}`}
              onChange={e => setVisionEdit(v => ({ ...v, api_key: e.target.value }))}
              placeholder="留空以保留当前值"
              type="password"
              value={visionEdit.api_key}
            />
            <div className="flex items-center gap-3">
              <Button disabled={visionSaving} onClick={handleSaveVisionSlot} variant="primary">
                {visionSaving ? '保存中...' : '保存'}
              </Button>
              {visionSlot?.api_key_present && (
                <button
                  className="text-xs text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)]"
                  onClick={handleClearVisionKey}
                >
                  清除密钥
                </button>
              )}
            </div>
          </div>

          {/* Fallback chain editor */}
          <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
            <p className="mb-1 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">备用模型链</p>
            <p className="mb-3 text-xs text-[var(--duties-secondary)]">
              主模型请求失败时按顺序尝试。基础 URL 和密钥留空时使用对应渠道的配置；是否可识图按备用模型自己的设置。
            </p>
            {fbRows.length === 0 && (
              <p className="mb-3 text-xs text-[var(--duties-tertiary)]">未配置备用模型。</p>
            )}
            {fbRows.map((row, i) => (
              <div key={row.key} className="mb-3 border border-[var(--duties-border)] p-3">
                <div className="mb-2 flex items-center justify-between">
                  <p className="font-mono text-xs font-semibold text-[var(--duties-text)]">#{i + 1}</p>
                  <div className="flex items-center gap-3 text-xs">
                    <button
                      className="text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)] disabled:opacity-40"
                      disabled={i === 0}
                      onClick={() => moveRow(i, -1)}
                    >
                      上移
                    </button>
                    <button
                      className="text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)] disabled:opacity-40"
                      disabled={i === fbRows.length - 1}
                      onClick={() => moveRow(i, 1)}
                    >
                      下移
                    </button>
                    <button className="text-red-400 underline hover:text-red-300" onClick={() => removeRow(row.key)}>
                      删除
                    </button>
                  </div>
                </div>
                <div className="grid gap-2 sm:grid-cols-2">
                  <div>
                    <label className="mb-1 block text-xs text-[var(--duties-secondary)]">渠道类型</label>
                    <select
                      className={inputCls}
                      onChange={e => updateRow(row.key, { provider: e.target.value })}
                      value={row.provider}
                    >
                      {Array.from(new Set([row.provider, ...(data?.registered || [])]))
                        .filter(Boolean)
                        .map(name => (
                          <option key={name} value={name}>{name}</option>
                        ))}
                    </select>
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-[var(--duties-secondary)]">模型</label>
                    <input
                      className={inputCls}
                      onChange={e =>
                        updateRow(row.key, { model: e.target.value, vision: modelVision(data, e.target.value) })
                      }
                      value={row.model}
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-[var(--duties-secondary)]">基础 URL</label>
                    <input
                      className={inputCls}
                      onChange={e => updateRow(row.key, { base_url: e.target.value })}
                      placeholder="留空使用渠道配置"
                      value={row.base_url}
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-[var(--duties-secondary)]">
                      API 密钥 {row.api_key_present && !row.clear_api_key ? `（已设置: ${row.api_key_redacted}）` : ''}
                    </label>
                    <input
                      className={inputCls}
                      onChange={e => updateRow(row.key, { api_key: e.target.value })}
                      placeholder={row.api_key_present ? '留空以保留当前值' : '留空使用渠道配置'}
                      type="password"
                      value={row.api_key}
                    />
                  </div>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-4 text-xs text-[var(--duties-secondary)]">
                  <label className="flex items-center gap-2">
                    <input
                      checked={row.vision}
                      disabled={!row.model.trim()}
                      onChange={e => updateRow(row.key, { vision: e.target.checked })}
                      type="checkbox"
                    />
                    可识图（vision）
                  </label>
                  {row.api_key_present && (
                    <label className="flex items-center gap-2">
                      <input
                        checked={Boolean(row.clear_api_key)}
                        onChange={e => updateRow(row.key, { clear_api_key: e.target.checked, api_key: '' })}
                        type="checkbox"
                      />
                      清除已保存的密钥
                    </label>
                  )}
                </div>
              </div>
            ))}
            <div className="flex items-center gap-3">
              <Button onClick={addRow}>添加备用模型</Button>
              <Button disabled={fbSaving || !fbDirty} onClick={handleSaveFallbacks} variant="primary">
                {fbSaving ? '保存中...' : '保存备用链'}
              </Button>
              {fbDirty && <span className="text-xs text-[var(--duties-tertiary)]">有未保存的修改</span>}
            </div>
          </div>

          {/* Reload */}
          <div className="flex items-center gap-3">
            <Button onClick={handleReload}>🔄 重载配置</Button>
            <p className="text-xs text-[var(--duties-secondary)]">重新读取 config.yaml，不中断服务</p>
          </div>
        </>
      )}
    </section>
  );
};
