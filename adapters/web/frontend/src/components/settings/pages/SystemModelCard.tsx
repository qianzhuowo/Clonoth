// A scheme + model reference editor: system_models.<slot> or node_models.<node_id>.
// Empty scheme = the current scheme; empty model = the scheme's own model.
import { useEffect, useState } from 'react';

import type { ChannelPublic, ChannelRef } from '../../../api/supervisorClient';
import { errorText, notify } from '../../../store/toastStore';
import { Button } from '../../common';

const inputCls =
  'w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]';

interface Props {
  title: string;
  description: string;
  channels: ChannelPublic[];
  activeChannel: string;
  value: ChannelRef | undefined;
  /** Text for the empty-scheme option; defaults to “当前方案”. */
  emptyLabel?: string;
  onSave: (ref: ChannelRef) => Promise<void>;
}

export const SystemModelCard = ({ title, description, channels, activeChannel, value, emptyLabel, onSave }: Props) => {
  const [edit, setEdit] = useState<ChannelRef>({ channel: '', model: '' });
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setEdit({ channel: value?.channel || '', model: value?.model || '' });
  }, [value?.channel, value?.model]);

  const active = channels.find(c => c.id === activeChannel);
  const picked = channels.find(c => c.id === (edit.channel || activeChannel));
  const dirty = edit.channel !== (value?.channel || '') || edit.model !== (value?.model || '');

  const save = async () => {
    setSaving(true);
    try {
      await onSave({ channel: edit.channel, model: edit.model.trim() });
      notify.success(`${title}已保存`);
    } catch (err) {
      notify.error(`${title}：${errorText(err, '保存失败')}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="border border-[var(--duties-border)] bg-[var(--duties-panel)] p-4">
      <p className="mb-1 font-mono text-[0.6rem] uppercase tracking-[0.2em] text-[var(--duties-tertiary)]">{title}</p>
      <p className="mb-3 text-xs text-[var(--duties-secondary)]">{description}</p>
      <div className="grid gap-2 sm:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs text-[var(--duties-secondary)]">渠道方案</label>
          <select className={inputCls} onChange={e => setEdit(v => ({ ...v, channel: e.target.value }))} value={edit.channel}>
            <option value="">{emptyLabel || `当前方案（${active?.name || '未设置'}）`}</option>
            {channels.map(c => (
              <option key={c.id} value={c.id}>{c.name}（{c.type}）</option>
            ))}
            {edit.channel && !channels.some(c => c.id === edit.channel) && (
              <option value={edit.channel}>{edit.channel}（不存在）</option>
            )}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs text-[var(--duties-secondary)]">模型</label>
          <input
            className={inputCls}
            onChange={e => setEdit(v => ({ ...v, model: e.target.value }))}
            placeholder={picked?.model ? `留空使用 ${picked.model}` : '留空使用方案的模型'}
            value={edit.model}
          />
        </div>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <Button disabled={saving || !dirty} onClick={save} variant="primary">
          {saving ? '保存中...' : '保存'}
        </Button>
        {dirty && <span className="text-xs text-[var(--duties-tertiary)]">有未保存的修改</span>}
      </div>
    </div>
  );
};
