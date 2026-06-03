// [2026-06-03] Model & Provider settings — config.yaml editor + reload/restart.
import { useEffect, useState } from 'react';

import {
  getConfigRaw,
  getModelConfig,
  reloadConfig,
  restartEngine,
  updateConfigRaw,
  updateModelConfig,
} from '../../../api/supervisorClient';
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

  // config.yaml raw editor
  const [configRaw, setConfigRaw] = useState('');
  const [configMsg, setConfigMsg] = useState('');
  const [configLoading, setConfigLoading] = useState(false);

  // action states
  const [reloading, setReloading] = useState(false);
  const [reloadMsg, setReloadMsg] = useState('');
  const [restarting, setRestarting] = useState(false);
  const [restartMsg, setRestartMsg] = useState('');

  useEffect(() => {
    if (!modelConfig) return;
    setModelEdit({ model: modelConfig.model || '', base_url: modelConfig.base_url || '', api_key: '' });
  }, [modelConfig?.base_url, modelConfig?.model]);

  const loadConfigRaw = async () => {
    if (!adminToken || !isAuthenticated) return;
    setConfigLoading(true);
    setConfigMsg('');
    try {
      const raw = await getConfigRaw(adminToken);
      setConfigRaw(raw);
      setConfigMsg('已加载');
    } catch (error) {
      setConfigMsg(error instanceof Error ? error.message : '加载失败');
    } finally {
      setConfigLoading(false);
    }
  };

  useEffect(() => {
    if (!adminToken || !isAuthenticated) return;
    getModelConfig(adminToken)
      .then(cfg => {
        setModelConfig(cfg);
        setGlobalConfig(cfg.model || '', cfg.base_url || '');
      })
      .catch(() => {});
    void loadConfigRaw();
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

  const handleConfigSave = async () => {
    if (!adminToken) return;
    if (!configRaw.trim()) {
      setConfigMsg('内容为空');
      return;
    }
    setConfigLoading(true);
    setConfigMsg('');
    try {
      await updateConfigRaw(adminToken, configRaw);
      setConfigMsg('已保存，点击「重载配置」使其生效');
    } catch (error) {
      setConfigMsg(error instanceof Error ? error.message : '保存失败');
    } finally {
      setConfigLoading(false);
    }
  };

  const handleReload = async () => {
    if (!adminToken) return;
    setReloading(true);
    setReloadMsg('');
    try {
      await reloadConfig(adminToken);
      // refresh model display
      const cfg = await getModelConfig(adminToken);
      setModelConfig(cfg);
      setGlobalConfig(cfg.model || '', cfg.base_url || '');
      setModelEdit({ model: cfg.model || '', base_url: cfg.base_url || '', api_key: '' });
      setReloadMsg('✅ 配置已重载');
    } catch (err) {
      setReloadMsg(err instanceof Error ? err.message : '重载失败');
    } finally {
      setReloading(false);
    }
  };

  const handleRestart = async () => {
    if (!adminToken) return;
    if (!window.confirm('确定要重启引擎吗？正在运行的任务可能会中断。')) return;
    setRestarting(true);
    setRestartMsg('');
    try {
      await restartEngine(adminToken);
      setRestartMsg('✅ 已触发引擎重启');
    } catch (err) {
      setRestartMsg(err instanceof Error ? err.message : '重启失败');
    } finally {
      setRestarting(false);
    }
  };

  return (
    <section className="mx-auto flex h-full max-w-4xl flex-col gap-5 overflow-y-auto p-4 sm:p-6">
      <div>
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">设置</p>
        <h2 className="mt-1 font-mono text-xl font-semibold tracking-[-0.04em]">模型</h2>
        <p className="mt-2 max-w-2xl text-sm text-[var(--duties-secondary)]">管理全局默认模型和 Provider 渠道配置（config.yaml）。</p>
      </div>

      {!isAuthenticated ? (
        <div className="border border-[var(--duties-border)] p-4 text-sm text-[var(--duties-secondary)]">
          修改模型设置前需要完成管理员令牌认证。
        </div>
      ) : (
        <>
          {/* Quick edit for active provider */}
          <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
            <p className="mb-3 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">当前活跃渠道（快捷编辑）</p>
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
              {modelSaving ? '保存中...' : '保存'}
            </Button>
          </div>

          {/* Full config.yaml editor */}
          <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
            <p className="mb-3 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">渠道配置（config.yaml）</p>
            <p className="mb-3 text-xs leading-5 text-[var(--duties-secondary)]">
              编辑完整的 config.yaml，包括多渠道 Provider、fallback 链等。保存后需点击「重载配置」使变更生效。
            </p>
            <YamlEditor
              height="16rem"
              onChange={setConfigRaw}
              placeholder={'version: 1\nprovider: openai\nopenai:\n  base_url: https://...\n  api_key: sk-...\n  model: gpt-4o\ndeepseek:\n  base_url: https://api.deepseek.com\n  api_key: sk-...\n  model: deepseek-chat\nfallbacks:\n- provider: deepseek'}
              value={configRaw}
            />
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <Button disabled={configLoading} onClick={loadConfigRaw}>
                {configLoading ? '处理中...' : '重新加载'}
              </Button>
              <Button disabled={configLoading || !configRaw.trim()} onClick={handleConfigSave} variant="primary">
                {configLoading ? '处理中...' : '保存'}
              </Button>
              {configMsg && <p className="text-xs text-[var(--duties-tertiary)]">{configMsg}</p>}
            </div>
          </div>

          {/* Actions: reload + restart */}
          <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
            <p className="mb-3 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">操作</p>
            <div className="flex flex-wrap items-center gap-3">
              <Button disabled={reloading} onClick={handleReload}>
                {reloading ? '重载中...' : '🔄 重载配置'}
              </Button>
              <Button disabled={restarting} onClick={handleRestart} variant="danger">
                {restarting ? '重启中...' : '⚡ 重启引擎'}
              </Button>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-4">
              {reloadMsg && <p className="text-xs text-[var(--duties-tertiary)]">{reloadMsg}</p>}
              {restartMsg && <p className="text-xs text-[var(--duties-tertiary)]">{restartMsg}</p>}
            </div>
            <p className="mt-2 text-xs leading-5 text-[var(--duties-secondary)]">
              重载配置：重新读取 config.yaml，不中断服务。重启引擎：完整重启后端引擎进程，运行中的任务可能中断。
            </p>
          </div>
        </>
      )}
    </section>
  );
};
