// System model slot card (data/config.yaml system_models.<slot>).
import { useEffect, useState } from 'react';

import { type SystemModelPublic, getSystemModel, updateSystemModel } from '../../../api/supervisorClient';
import { Button } from '../../common';

const inputCls =
  'w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]';

interface Props {
  adminToken: string;
  slot: string;
  title: string;
  description: string;
  /** Configured channel names; omit for slots that do not pick a channel (vision). */
  providers?: string[];
  activeProvider?: string;
  modelPlaceholder: string;
  envHint: string;
  onMessage: (msg: string) => void;
}

export const SystemModelCard = ({
  adminToken,
  slot,
  title,
  description,
  providers,
  activeProvider,
  modelPlaceholder,
  envHint,
  onMessage,
}: Props) => {
  const [current, setCurrent] = useState<SystemModelPublic | null>(null);
  const [edit, setEdit] = useState({ provider: '', model: '', base_url: '', api_key: '' });
  const [saving, setSaving] = useState(false);

  const apply = (s: SystemModelPublic) => {
    setCurrent(s);
    setEdit({ provider: s.provider, model: s.model, base_url: s.base_url, api_key: '' });
  };

  useEffect(() => {
    getSystemModel(adminToken, slot).then(apply).catch(err => onMessage(err instanceof Error ? err.message : '加载失败'));
  }, [adminToken, slot]);

  const save = async (payload: { provider?: string; model?: string; base_url?: string; api_key?: string }, done: string) => {
    setSaving(true);
    try {
      apply(await updateSystemModel(adminToken, slot, payload));
      onMessage(done);
    } catch (err) {
      onMessage(err instanceof Error ? err.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleSave = () => {
    const payload: { provider?: string; model: string; base_url: string; api_key?: string } = {
      model: edit.model,
      base_url: edit.base_url,
    };
    if (providers) payload.provider = edit.provider;
    if (edit.api_key) payload.api_key = edit.api_key;
    void save(payload, `${title}已保存`);
  };

  const inheritFrom = providers
    ? (edit.provider ? `${edit.provider} 渠道` : `当前渠道 ${activeProvider || ''}`)
    : envHint;

  return (
    <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
      <p className="mb-1 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">{title}</p>
      <p className="mb-3 text-xs text-[var(--duties-secondary)]">{description}</p>
      {providers && (
        <>
          <label className="mb-1 block text-xs text-[var(--duties-secondary)]">渠道</label>
          <select
            className={`mb-3 ${inputCls}`}
            onChange={e => setEdit(v => ({ ...v, provider: e.target.value }))}
            value={edit.provider}
          >
            <option value="">跟随当前渠道（{activeProvider || '未设置'}）</option>
            {providers.map(name => (
              <option key={name} value={name}>{name}</option>
            ))}
            {edit.provider && !providers.includes(edit.provider) && (
              <option value={edit.provider}>{edit.provider}（未配置）</option>
            )}
          </select>
        </>
      )}
      <label className="mb-1 block text-xs text-[var(--duties-secondary)]">模型</label>
      <input
        className={`mb-3 ${inputCls}`}
        onChange={e => setEdit(v => ({ ...v, model: e.target.value }))}
        placeholder={modelPlaceholder}
        value={edit.model}
      />
      <label className="mb-1 block text-xs text-[var(--duties-secondary)]">基础 URL</label>
      <input
        className={`mb-3 ${inputCls}`}
        onChange={e => setEdit(v => ({ ...v, base_url: e.target.value }))}
        placeholder={`留空使用${inheritFrom}的地址`}
        value={edit.base_url}
      />
      <label className="mb-1 block text-xs text-[var(--duties-secondary)]">
        API 密钥 {current?.api_key_present ? `（已设置: ${current.api_key_redacted}）` : `（未设置，使用${inheritFrom}的密钥）`}
      </label>
      <input
        className={`mb-3 ${inputCls}`}
        onChange={e => setEdit(v => ({ ...v, api_key: e.target.value }))}
        placeholder="留空以保留当前值"
        type="password"
        value={edit.api_key}
      />
      <div className="flex items-center gap-3">
        <Button disabled={saving} onClick={handleSave} variant="primary">
          {saving ? '保存中...' : '保存'}
        </Button>
        {current?.api_key_present && (
          <button
            className="text-xs text-[var(--duties-secondary)] underline hover:text-[var(--duties-text)]"
            onClick={() => void save({ api_key: '' }, `${title}密钥已清除`)}
          >
            清除密钥
          </button>
        )}
      </div>
    </div>
  );
};
