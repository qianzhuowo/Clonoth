// [2026-06-01] Global model settings page — default model / base_url / api_key.
// Runtime and provider configuration lives in the Advanced settings tab.
import { useEffect, useState } from 'react';

import { getModelConfig, updateModelConfig } from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';

interface ModelEditState {
  model: string;
  base_url: string;
  api_key: string;
}

export const ModelSettingsPage = () => {
  const {
    adminToken,
    isAuthenticated,
    modelConfig,
    setGlobalConfig,
    setModelConfig,
  } = useSettingsStore();
  const [modelEdit, setModelEdit] = useState<ModelEditState>({ model: '', base_url: '', api_key: '' });
  const [modelSaving, setModelSaving] = useState(false);
  const [modelMsg, setModelMsg] = useState('');

  useEffect(() => {
    // [2026-06-01] Hydrate the edit form from the shared global model cache.
    // Why: the cache can be filled by LoginPage, General settings, or this page.
    // How: copy model and base_url into local form state whenever the loaded config
    // changes, leaving api_key blank for safety. Purpose: secrets are never echoed.
    if (!modelConfig) return;
    setModelEdit({ model: modelConfig.model || '', base_url: modelConfig.base_url || '', api_key: '' });
  }, [modelConfig?.base_url, modelConfig?.model]);

  useEffect(() => {
    // [2026-06-01] Load global model config when the Model tab is opened directly.
    // Why: Header model clicks can enter this tab without visiting General first.
    // How: call the existing admin-protected config endpoint if a token exists.
    // Purpose: the page remains independently navigable.
    if (!adminToken || !isAuthenticated) return;
    getModelConfig(adminToken)
      .then(cfg => {
        setModelConfig(cfg);
        setGlobalConfig(cfg.model || '', cfg.base_url || '');
      })
      .catch(() => { /* leave the form editable with existing cached values */ });
  }, [adminToken, isAuthenticated, setGlobalConfig, setModelConfig]);

  const handleModelSave = async () => {
    if (!adminToken) return;
    setModelSaving(true);
    setModelMsg('');
    try {
      const params: Record<string, string> = {};
      if (modelEdit.model) params.model = modelEdit.model;
      if (modelEdit.base_url) params.base_url = modelEdit.base_url;
      if (modelEdit.api_key) params.api_key = modelEdit.api_key;
      await updateModelConfig(adminToken, params);
      const cfg = await getModelConfig(adminToken);
      setModelConfig(cfg);
      setGlobalConfig(cfg.model || '', cfg.base_url || '');
      setModelEdit({ model: cfg.model || '', base_url: cfg.base_url || '', api_key: '' });
      setModelMsg('已保存');
    } catch (err) {
      setModelMsg(err instanceof Error ? err.message : '失败');
    } finally {
      setModelSaving(false);
    }
  };

  return (
    <section className="mx-auto flex h-full max-w-4xl flex-col gap-5 overflow-y-auto p-4 sm:p-6">
      <div>
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">设置</p>
        <h2 className="mt-1 font-mono text-xl font-semibold tracking-[-0.04em]">模型</h2>
        <p className="mt-2 max-w-2xl text-sm text-[var(--duties-secondary)]">配置全局默认模型、基础 URL 和 API 密钥。运行时配置请在高级设置中编辑。</p>
      </div>

      {!isAuthenticated ? (
        <div className="border border-[var(--duties-border)] p-4 text-sm text-[var(--duties-secondary)]">
          修改模型设置前需要完成管理员令牌认证。
        </div>
      ) : (
        <>
          <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
            <p className="mb-3 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">全局默认配置</p>
            <label className="mb-1 block text-xs text-[var(--duties-secondary)]">模型</label>
            <input
              className="mb-3 w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
              onChange={e => setModelEdit(p => ({ ...p, model: e.target.value }))}
              value={modelEdit.model}
            />
            <label className="mb-1 block text-xs text-[var(--duties-secondary)]">基础 URL</label>
            <input
              className="mb-3 w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
              onChange={e => setModelEdit(p => ({ ...p, base_url: e.target.value }))}
              value={modelEdit.base_url}
            />
            <label className="mb-1 block text-xs text-[var(--duties-secondary)]">API 密钥 {modelConfig?.api_key_present ? '（已设置）' : '（未设置）'}</label>
            <input
              className="mb-3 w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
              onChange={e => setModelEdit(p => ({ ...p, api_key: e.target.value }))}
              placeholder="留空以保留当前值"
              type="password"
              value={modelEdit.api_key}
            />
            {modelMsg && <p className="mb-3 text-xs text-[var(--duties-tertiary)]">{modelMsg}</p>}
            <Button disabled={modelSaving} onClick={handleModelSave} variant="primary">
              {modelSaving ? '保存中...' : '保存默认配置'}
            </Button>
          </div>
        </>
      )}
    </section>
  );
};
