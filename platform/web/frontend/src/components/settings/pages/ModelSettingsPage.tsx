// [2026-06-01] Global model settings page split from the legacy SettingsPanel.
// Why: global provider configuration should live in a dedicated settings tab while
// session-specific overrides remain in the chat/session panel. How: load and save
// the existing OpenAI config endpoints, and edit runtime.yaml providers through the
// raw runtime config endpoints. Purpose: model settings can manage both the default
// OpenAI-compatible channel and provider-specific runtime options.
import { useEffect, useState } from 'react';

import { getModelConfig, getRuntimeRaw, updateModelConfig, updateRuntimeRaw } from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button, YamlEditor } from '../../common';

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
  // [2026-06-02] Keep the Provider area backed by the complete runtime.yaml text.
  // Why: provider settings share one runtime file with shell and memory settings. How:
  // store the raw YAML plus one status/loading pair. Purpose: the editor can load and
  // save the full runtime configuration without splitting sections in this page.
  const [runtimeRaw, setRuntimeRaw] = useState('');
  const [runtimeMsg, setRuntimeMsg] = useState('');
  const [runtimeLoading, setRuntimeLoading] = useState(false);

  useEffect(() => {
    // [2026-06-01] Hydrate the edit form from the shared global model cache.
    // Why: the cache can be filled by LoginPage, General settings, or this page.
    // How: copy model and base_url into local form state whenever the loaded config
    // changes, leaving api_key blank for safety. Purpose: secrets are never echoed.
    if (!modelConfig) return;
    setModelEdit({ model: modelConfig.model || '', base_url: modelConfig.base_url || '', api_key: '' });
  }, [modelConfig?.base_url, modelConfig?.model]);

  const loadRuntimeRaw = async () => {
    if (!adminToken || !isAuthenticated) return;
    setRuntimeLoading(true);
    setRuntimeMsg('');
    try {
      // [2026-06-02] Load the full runtime.yaml before Provider edits.
      // Why: providers, shell, memory, and other runtime settings live in the same
      // file. How: call the raw runtime endpoint and place its content directly into
      // YamlEditor. Purpose: admins can review the complete runtime context before
      // saving Provider-related changes.
      const raw = await getRuntimeRaw(adminToken);
      setRuntimeRaw(raw);
      setRuntimeMsg('已加载 runtime.yaml');
    } catch (error) {
      setRuntimeMsg(error instanceof Error ? error.message : '加载运行时配置失败');
    } finally {
      setRuntimeLoading(false);
    }
  };

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
    void loadRuntimeRaw();
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

  const handleRuntimeSave = async () => {
    if (!adminToken) return;
    if (!runtimeRaw.trim()) {
      setRuntimeMsg('请先加载或填写 runtime.yaml 内容');
      return;
    }
    setRuntimeLoading(true);
    setRuntimeMsg('');
    try {
      // [2026-06-02] Save the editor content as the complete runtime.yaml file.
      // Why: this Provider configuration area now intentionally exposes the full
      // runtime document, not only the providers subsection. How: send the raw editor
      // string unchanged to the runtime raw update endpoint. Purpose: admins can update
      // providers while preserving visibility into shell, memory, and other settings.
      await updateRuntimeRaw(adminToken, runtimeRaw);
      setRuntimeMsg('运行时配置已保存');
    } catch (error) {
      setRuntimeMsg(error instanceof Error ? error.message : '保存运行时配置失败');
    } finally {
      setRuntimeLoading(false);
    }
  };

  return (
    <section className="mx-auto flex h-full max-w-4xl flex-col gap-5 overflow-y-auto p-4 sm:p-6">
      <div>
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">设置</p>
        <h2 className="mt-1 font-mono text-xl font-semibold tracking-[-0.04em]">模型</h2>
        <p className="mt-2 max-w-2xl text-sm text-[var(--duties-secondary)]">配置全局默认模型、基础 URL 和 runtime.yaml 中的 Provider 运行参数。</p>
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

          <div className="mt-6 border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
            <p className="mb-3 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">运行时配置</p>
            <p className="mb-3 text-xs leading-5 text-[var(--duties-secondary)]">
              此处编辑 runtime.yaml 的完整内容，包括 providers、shell、memory 等全局配置。
            </p>
            <div className="mb-3 flex flex-wrap gap-2">
              <Button disabled={runtimeLoading} onClick={loadRuntimeRaw}>{runtimeLoading ? '加载中...' : '加载'}</Button>
            </div>
            {/* [2026-06-02] This editor intentionally shows the full runtime.yaml file.
                Why: the requested Provider area must include providers, shell, memory,
                and other global runtime settings. How: bind YamlEditor directly to the
                raw runtime state with a 20rem height. Purpose: users can edit the whole
                runtime document from one consistent settings card. */}
            <YamlEditor height="20rem" onChange={setRuntimeRaw} placeholder="providers:\n  openai:\n    timeout_sec: 600\nshell:\n  timeout_sec: 30\nmemory: {}" value={runtimeRaw} />
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <Button disabled={runtimeLoading || !runtimeRaw.trim()} onClick={handleRuntimeSave} variant="primary">
                {runtimeLoading ? '处理中...' : '保存'}
              </Button>
              {runtimeMsg && <p className="text-xs text-[var(--duties-tertiary)]">{runtimeMsg}</p>}
            </div>
          </div>
        </>
      )}
    </section>
  );
};
