// One channel = one job: its own format, address, key and model, edited in place.
// Role channels (vision, compaction, image ...) carry a switch; main, fallbacks and
// other channels are always on.
import { type ReactNode, useEffect, useState } from 'react';

import {
  type ChannelPublic,
  type ChannelsResponse,
  type ChannelUpdate,
  pullChannelModels,
  setModelVision,
  upsertChannel,
} from '../../../api/supervisorClient';
import { errorText, notify } from '../../../store/toastStore';
import { Button } from '../../common';

export const inputCls =
  'w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]';
const labelCls = 'mb-1 block text-xs text-[var(--duties-secondary)]';

/** Free text input with the pulled model list as suggestions. */
export const ModelInput = ({ value, onChange, options, placeholder }: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
  placeholder?: string;
}) => {
  const [open, setOpen] = useState(false);
  const q = value.trim().toLowerCase();
  // An exact pick shows the whole list again, typing narrows it.
  const shown = !q || options.includes(value) ? options : options.filter(m => m.toLowerCase().includes(q));
  return (
    <div className="relative">
      <input
        className={inputCls}
        onBlur={() => setOpen(false)}
        onChange={e => { onChange(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        placeholder={placeholder}
        value={value}
      />
      {open && shown.length > 0 && (
        <ul className="absolute left-0 right-0 z-30 mt-1 max-h-64 overflow-y-auto border border-[var(--duties-border)] bg-[var(--duties-panel)] shadow-lg">
          {shown.map(m => (
            <li key={m}>
              <button
                className={`block w-full px-3 py-1.5 text-left font-mono text-xs hover:bg-[var(--duties-bg)] ${m === value ? 'font-semibold text-[var(--duties-text)]' : 'text-[var(--duties-secondary)]'}`}
                onMouseDown={e => { e.preventDefault(); onChange(m); setOpen(false); }}
                type="button"
              >
                {m}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};

export const Switch = ({ checked, onChange, disabled, label }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  label: string;
}) => (
  <button
    aria-checked={checked}
    className="inline-flex items-center gap-2 text-xs text-[var(--duties-secondary)] disabled:opacity-50"
    disabled={disabled}
    onClick={() => onChange(!checked)}
    role="switch"
    type="button"
  >
    <span>{label}</span>
    <span className={`relative inline-block h-5 w-9 border transition-colors ${checked ? 'border-[var(--duties-text)] bg-[var(--duties-text)]' : 'border-[var(--duties-border)]'}`}>
      <span className={`absolute top-[3px] h-3 w-3 transition-all ${checked ? 'left-[19px] bg-[var(--duties-bg)]' : 'left-[3px] bg-[var(--duties-tertiary)]'}`} />
    </span>
  </button>
);

interface Draft {
  name: string;
  type: string;
  base_url: string;
  model: string;
  api_key: string;
}

export interface RoleChannelCardProps {
  token: string;
  id: string;
  title: string;
  description: string;
  channel?: ChannelPublic;
  main?: ChannelPublic;
  registered: string[];
  /** Format for a new channel; defaults to the main model's. */
  defaultType?: string;
  /** follow: off = use the main model; enable: off = the tool is unavailable. */
  toggle?: 'follow' | 'enable';
  nameEditable?: boolean;
  showVision?: boolean;
  actions?: ReactNode;
  onChanged: (resp: ChannelsResponse) => void;
  /** Runs after the channel is first created (e.g. append it to the fallback chain). */
  afterCreate?: (resp: ChannelsResponse) => Promise<ChannelsResponse>;
}

const draftFor = (p: RoleChannelCardProps): Draft => {
  if (p.channel) {
    return { name: p.channel.name, type: p.channel.type, base_url: p.channel.base_url, model: p.channel.model, api_key: '' };
  }
  const type = p.defaultType || p.main?.type || 'openai';
  return { name: p.title, type, base_url: type === p.main?.type ? p.main?.base_url || '' : '', model: '', api_key: '' };
};

export const RoleChannelCard = (props: RoleChannelCardProps) => {
  const { token, id, title, description, channel, main, registered, toggle, nameEditable, showVision, actions, onChanged, afterCreate } = props;
  const [draft, setDraft] = useState<Draft>(() => draftFor(props));
  const [opened, setOpened] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState('');

  useEffect(() => {
    setDraft(draftFor(props));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel?.name, channel?.type, channel?.base_url, channel?.model, main?.type, main?.base_url]);

  const on = !toggle || (channel ? channel.enabled : opened);
  const copyKey = !channel && id !== 'main' && !!main?.api_key_present;
  const dirty = !channel || draft.api_key.trim() !== '' || draft.type !== channel.type
    || draft.base_url !== channel.base_url || draft.model !== channel.model || (!!nameEditable && draft.name !== channel.name);
  const types = registered.includes(draft.type) || !draft.type ? registered : [...registered, draft.type];
  const set = (patch: Partial<Draft>) => setDraft(d => ({ ...d, ...patch }));

  const run = async (what: string, fn: () => Promise<void>) => {
    setBusy(what);
    try {
      await fn();
    } catch (err) {
      notify.error(`${title}：${errorText(err, '操作失败')}`);
    } finally {
      setBusy('');
    }
  };

  const save = () => run('save', async () => {
    if (!draft.type) throw new Error('请选择格式');
    const data: ChannelUpdate = { type: draft.type, base_url: draft.base_url.trim(), model: draft.model.trim() };
    if (nameEditable) data.name = draft.name.trim() || id;
    if (draft.api_key.trim()) data.api_key = draft.api_key.trim();
    else if (copyKey) data.copy_key_from = 'main';
    if (toggle) data.enabled = true;
    let resp = await upsertChannel(token, id, data);
    if (!channel && afterCreate) resp = await afterCreate(resp);
    onChanged(resp);
    setOpened(false);
    notify.success(`${title}已保存`);
  });

  const switchTo = (value: boolean) => {
    if (!channel) { setOpened(value); return; }
    void run('switch', async () => onChanged(await upsertChannel(token, id, { enabled: value })));
  };

  const pull = () => run('pull', async () => {
    const useSaved = channel ? id : (copyKey && !draft.api_key.trim() ? 'main' : '');
    const out = await pullChannelModels(token, {
      channel: useSaved || undefined, type: draft.type, base_url: draft.base_url.trim(), api_key: draft.api_key.trim(),
    });
    setModels(out.models);
    notify.success(`拉到 ${out.models.length} 个模型，可从列表选择或直接输入`);
  });

  const clearKey = () => run('clear', async () => {
    if (!window.confirm(`清除「${title}」保存的密钥？`)) return;
    onChanged(await upsertChannel(token, id, { clear_api_key: true }));
  });

  const toggleVision = (vision: boolean) => run('vision', async () => {
    if (channel?.model) onChanged(await setModelVision(token, channel.model, vision));
  });

  const offText = toggle === 'follow'
    ? `跟随主模型（${main?.model || '主模型未设置'}）${channel ? '，单独设置已保留' : ''}`
    : channel ? '已关闭，对应工具不可用（设置已保留）' : '未启用，对应工具不可用';

  return (
    <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <p className="font-mono text-[0.65rem] uppercase tracking-[0.2em] text-[var(--duties-text)]">
          {nameEditable && channel ? channel.name : title}
          <span className="ml-2 normal-case tracking-normal text-[var(--duties-tertiary)]">{id}</span>
        </p>
        <div className="flex items-center gap-3">
          {toggle && (
            <Switch
              checked={on}
              disabled={!!busy}
              label={toggle === 'follow' ? '单独设置' : '启用'}
              onChange={switchTo}
            />
          )}
          {actions}
        </div>
      </div>
      <p className="mb-3 text-xs text-[var(--duties-secondary)]">{description}</p>

      {!on ? (
        <p className="font-mono text-xs text-[var(--duties-tertiary)]">{offText}</p>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            {nameEditable && (
              <div className="sm:col-span-2">
                <label className={labelCls}>名称</label>
                <input className={inputCls} onChange={e => set({ name: e.target.value })} value={draft.name} />
              </div>
            )}
            <div>
              <label className={labelCls}>格式</label>
              <select className={inputCls} onChange={e => set({ type: e.target.value })} value={draft.type}>
                {types.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div>
              <label className={labelCls}>API 密钥</label>
              <input
                className={inputCls}
                onChange={e => set({ api_key: e.target.value })}
                placeholder={channel
                  ? (channel.api_key_present ? `已保存 ${channel.api_key_redacted}，留空不变` : '未设置')
                  : (copyKey ? '留空则沿用主模型的密钥' : '')}
                type="password"
                value={draft.api_key}
              />
            </div>
            <div className="sm:col-span-2">
              <label className={labelCls}>基础 URL</label>
              <input
                className={inputCls}
                onChange={e => set({ base_url: e.target.value })}
                placeholder={draft.type === 'gemini' ? 'https://.../v1beta' : 'https://.../v1'}
                value={draft.base_url}
              />
              {channel?.base_url_resolved && channel.base_url_resolved !== channel.base_url && (
                <p className="mt-1 font-mono text-[0.65rem] text-[var(--duties-tertiary)]">实际地址：{channel.base_url_resolved}</p>
              )}
              {channel?.type_hint && <p className="mt-1 text-xs text-amber-500">{channel.type_hint}</p>}
            </div>
            <div className="sm:col-span-2">
              <label className={labelCls}>模型{models.length > 0 && `（已拉取 ${models.length} 个，可选可填）`}</label>
              <div className="flex gap-2">
                <div className="min-w-0 flex-1">
                  <ModelInput onChange={v => set({ model: v })} options={models} placeholder="模型名，可直接输入" value={draft.model} />
                </div>
                <Button disabled={!!busy} onClick={pull}>{busy === 'pull' ? '拉取中...' : '拉取模型'}</Button>
              </div>
            </div>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Button disabled={!!busy || !dirty} onClick={save} variant="primary">
              {busy === 'save' ? '保存中...' : channel ? '保存' : '创建并启用'}
            </Button>
            {channel?.api_key_present && <Button disabled={!!busy} onClick={clearKey}>清除密钥</Button>}
            {showVision && channel?.model && (
              <label className="inline-flex items-center gap-2 text-xs text-[var(--duties-secondary)]">
                <input checked={channel.vision} disabled={!!busy} onChange={e => toggleVision(e.target.checked)} type="checkbox" />
                该模型能直接看图
              </label>
            )}
            {channel && dirty && <span className="text-xs text-[var(--duties-tertiary)]">有未保存的修改</span>}
          </div>
        </>
      )}
    </div>
  );
};
