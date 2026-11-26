// Model settings laid out by job: main model, vision, fallback chain, compaction /
// summary / memory, the two image tools, then other channels picked by nodes and
// draw presets. Each job owns one channel with its own format, address, key and model.
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  type ChannelPublic,
  type ChannelsResponse,
  deleteChannel,
  getChannels,
  reloadConfig,
  updateFallbacks,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { errorText, notify } from '../../../store/toastStore';
import { Button } from '../../common';
import { inputCls, RoleChannelCard } from './RoleChannelCard';

const CONFIG_RELOADED = 'clonoth:config-reloaded';

/** Header button: re-read data/config.yaml and refresh the page. */
export const ModelHeaderActions = () => {
  const adminToken = useSettingsStore(s => s.adminToken);
  const [busy, setBusy] = useState(false);
  if (!adminToken) return null;
  const reload = async () => {
    setBusy(true);
    try {
      await reloadConfig(adminToken);
      window.dispatchEvent(new Event(CONFIG_RELOADED));
      notify.success('配置已重载');
    } catch (err) {
      notify.error(errorText(err, '重载失败'));
    } finally {
      setBusy(false);
    }
  };
  return <Button className="h-8 flex-shrink-0" disabled={busy} onClick={reload}>{busy ? '重载中...' : '重载配置'}</Button>;
};

interface RoleInfo {
  title: string;
  description: string;
  toggle?: 'follow' | 'enable';
  defaultType?: string;
}

const ROLES: Record<string, RoleInfo> = {
  main: { title: '主模型', description: '对话默认使用；节点没有单独指定渠道时都用它。' },
  vision: {
    title: '识图模型', toggle: 'follow', defaultType: 'gemini',
    description: 'read_media 识图工具，以及不能看图的模型收到图片时的自动转写。需要 Gemini 格式。关闭“单独设置”则用主模型。',
  },
  compact: { title: '上下文压缩模型', toggle: 'follow', description: '会话超过压缩阈值时把旧消息压成摘要。关闭“单独设置”则用主模型。' },
  summary: { title: '轮次摘要模型', toggle: 'follow', description: '每轮任务结束后生成简短执行摘要。关闭“单独设置”则用主模型。' },
  memory: { title: '记忆提取模型', toggle: 'follow', description: '从对话里提取值得长期保存的信息。关闭“单独设置”则用主模型。' },
  image: {
    title: 'Gemini 生图模型', toggle: 'enable', defaultType: 'gemini',
    description: 'gemini_image 生图工具（二次元 / 插画 / 按宽高比）。需要 Gemini 格式。关闭后该工具不可用。',
  },
  image_gpt: {
    title: 'GPT 生图模型', toggle: 'enable', defaultType: 'openai',
    description: 'gpt_image_2 生图工具（OpenAI 图片接口，如 gpt-image-2）。关闭后该工具不可用。',
  },
};

const SECTIONS: { key: string; label: string; group: number }[] = [
  { key: 'main', label: '主模型', group: 0 },
  { key: 'vision', label: '识图模型', group: 0 },
  { key: 'fallback', label: '备用链路', group: 0 },
  { key: 'compact', label: '上下文压缩', group: 1 },
  { key: 'summary', label: '轮次摘要', group: 1 },
  { key: 'memory', label: '记忆提取', group: 1 },
  { key: 'image', label: 'Gemini 生图', group: 2 },
  { key: 'image_gpt', label: 'GPT 生图', group: 2 },
  { key: 'other', label: '其他渠道', group: 3 },
];

const secId = (key: string) => `model-sec-${key}`;

const SectionTitle = ({ title, hint }: { title: string; hint?: string }) => (
  <div className="mb-2">
    <h3 className="font-mono text-sm font-semibold tracking-[-0.02em]">{title}</h3>
    {hint && <p className="mt-0.5 text-xs text-[var(--duties-secondary)]">{hint}</p>}
  </div>
);

export const ModelSettingsPage = () => {
  const adminToken = useSettingsStore(s => s.adminToken);
  const setChannelConfig = useSettingsStore(s => s.setChannelConfig);
  const [data, setData] = useState<ChannelsResponse | null>(null);
  const [loadError, setLoadError] = useState('');
  const [pendingFallback, setPendingFallback] = useState('');
  const [pendingOther, setPendingOther] = useState('');
  const [newOtherId, setNewOtherId] = useState('');
  const [current, setCurrent] = useState('main');
  const scrollRef = useRef<HTMLElement>(null);

  const apply = useCallback((resp: ChannelsResponse) => {
    setData(resp);
    setChannelConfig(resp.channels.map(c => ({ id: c.id, name: c.name, type: c.type, model: c.model })));
  }, [setChannelConfig]);

  const load = useCallback(async () => {
    if (!adminToken) return;
    try {
      apply(await getChannels(adminToken));
      setLoadError('');
    } catch (err) {
      setLoadError(errorText(err, '加载失败'));
    }
  }, [adminToken, apply]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const onReload = () => { void load(); };
    window.addEventListener(CONFIG_RELOADED, onReload);
    return () => window.removeEventListener(CONFIG_RELOADED, onReload);
  }, [load]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 4) {
      setCurrent(SECTIONS[SECTIONS.length - 1].key);
      return;
    }
    const top = el.getBoundingClientRect().top;
    let cur = SECTIONS[0].key;
    for (const s of SECTIONS) {
      const sec = document.getElementById(secId(s.key));
      if (sec && sec.getBoundingClientRect().top - top <= 96) cur = s.key;
    }
    setCurrent(cur);
  };

  const jump = (key: string) => {
    document.getElementById(secId(key))?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setCurrent(key);
  };

  if (!adminToken) {
    return <section className="p-6 text-sm text-[var(--duties-secondary)]">请先登录管理员令牌。</section>;
  }
  if (!data) {
    return <section className="p-6 text-sm text-[var(--duties-secondary)]">{loadError || '加载中...'}</section>;
  }

  const token = adminToken;
  const byId: Record<string, ChannelPublic> = Object.fromEntries(data.channels.map(c => [c.id, c]));
  const main = byId.main;
  const registered = data.registered || [];
  const fallbacks = data.fallbacks.map(id => byId[id]).filter(Boolean);
  const others = data.channels.filter(c => c.role === 'other');

  const run = async (fn: () => Promise<ChannelsResponse>) => {
    try {
      apply(await fn());
    } catch (err) {
      notify.error(errorText(err, '操作失败'));
    }
  };

  const moveFallback = (i: number, dir: -1 | 1) => {
    const ids = [...data.fallbacks];
    [ids[i], ids[i + dir]] = [ids[i + dir], ids[i]];
    void run(() => updateFallbacks(token, ids));
  };

  const removeChannel = (c: ChannelPublic) => {
    if (!window.confirm(`删除「${c.name}」（${c.id}）？`)) return;
    void run(() => deleteChannel(token, c.id));
  };

  const addFallback = () => {
    let n = 1;
    while (byId[`fallback-${n}`] || data.fallbacks.includes(`fallback-${n}`)) n += 1;
    setPendingFallback(`fallback-${n}`);
  };

  const addOther = () => {
    const id = newOtherId.trim();
    if (!/^[A-Za-z0-9_.-]+$/.test(id)) { notify.error('渠道 id 只能用字母、数字、_ . -'); return; }
    if (byId[id] || ROLES[id] || id.startsWith('fallback-')) { notify.error('这个 id 已被占用或属于固定角色'); return; }
    setPendingOther(id);
    setNewOtherId('');
  };

  const role = (key: string) => {
    const info = ROLES[key];
    return (
      <section className="scroll-mt-4" id={secId(key)} key={key}>
        <RoleChannelCard
          channel={byId[key]}
          defaultType={info.defaultType}
          description={info.description}
          id={key}
          main={main}
          onChanged={apply}
          registered={registered}
          showVision={key === 'main'}
          title={info.title}
          toggle={info.toggle}
          token={token}
        />
      </section>
    );
  };

  const divider = <hr className="border-[var(--duties-border)]" />;

  return (
    <div className="flex h-full min-h-0">
      <nav className="hidden w-40 flex-shrink-0 overflow-y-auto border-r border-[var(--duties-border)] py-4 md:block">
        {SECTIONS.map((s, i) => (
          <div key={s.key}>
            {i > 0 && SECTIONS[i - 1].group !== s.group && <div className="mx-3 my-2 border-t border-[var(--duties-border)]" />}
            <button
              className={`block w-full border-l-2 px-3 py-1.5 text-left font-mono text-xs transition-colors ${current === s.key
                ? 'border-[var(--duties-text)] text-[var(--duties-text)]'
                : 'border-transparent text-[var(--duties-tertiary)] hover:text-[var(--duties-text)]'}`}
              onClick={() => jump(s.key)}
              type="button"
            >
              {s.label}
            </button>
          </div>
        ))}
      </nav>

      <section className="min-h-0 min-w-0 flex-1 overflow-y-auto p-4 sm:p-6" onScroll={onScroll} ref={scrollRef}>
        <div className="mx-auto max-w-3xl space-y-5">
          <div className="flex flex-wrap gap-1 md:hidden">
            {SECTIONS.map(s => (
              <button
                className="border border-[var(--duties-border)] px-2 py-1 font-mono text-[0.65rem] text-[var(--duties-secondary)]"
                key={s.key}
                onClick={() => jump(s.key)}
                type="button"
              >
                {s.label}
              </button>
            ))}
          </div>

          {role('main')}
          {role('vision')}

          <section className="scroll-mt-4 space-y-3" id={secId('fallback')}>
            <SectionTitle hint="主模型请求失败时按顺序逐条尝试。每条链路有自己的格式、地址、密钥和模型。" title="备用链路" />
            {fallbacks.length === 0 && !pendingFallback && (
              <p className="text-xs text-[var(--duties-tertiary)]">还没有备用链路。</p>
            )}
            {fallbacks.map((c, i) => (
              <RoleChannelCard
                actions={(
                  <>
                    <Button className="h-8" disabled={i === 0} onClick={() => moveFallback(i, -1)}>上移</Button>
                    <Button className="h-8" disabled={i === fallbacks.length - 1} onClick={() => moveFallback(i, 1)}>下移</Button>
                    <Button className="h-8" onClick={() => removeChannel(c)}>删除</Button>
                  </>
                )}
                channel={c}
                description={`第 ${i + 1} 条备用`}
                id={c.id}
                key={c.id}
                main={main}
                nameEditable
                onChanged={apply}
                registered={registered}
                showVision
                title={c.name}
                token={token}
              />
            ))}
            {pendingFallback && (
              <RoleChannelCard
                actions={<Button className="h-8" onClick={() => setPendingFallback('')}>取消</Button>}
                afterCreate={() => updateFallbacks(token, [...data.fallbacks, pendingFallback])}
                description="新的备用链路，保存后加到链路末尾。"
                id={pendingFallback}
                key={pendingFallback}
                main={main}
                nameEditable
                onChanged={resp => { apply(resp); setPendingFallback(''); }}
                registered={registered}
                title={`备用 ${data.fallbacks.length + 1}`}
                token={token}
              />
            )}
            {!pendingFallback && <Button onClick={addFallback}>添加备用链路</Button>}
          </section>

          {divider}
          {role('compact')}
          {role('summary')}
          {role('memory')}

          {divider}
          {role('image')}
          {role('image_gpt')}

          {divider}
          <section className="scroll-mt-4 space-y-3" id={secId('other')}>
            <SectionTitle hint="给节点模型、绘图面板的场景 Agent 预设选用的渠道（例如「绘图分析」）。" title="其他渠道" />
            {others.map(c => (
              <RoleChannelCard
                actions={<Button className="h-8" onClick={() => removeChannel(c)}>删除</Button>}
                channel={c}
                description="可在节点设置、绘图面板里选用。"
                id={c.id}
                key={c.id}
                main={main}
                nameEditable
                onChanged={apply}
                registered={registered}
                showVision
                title={c.name}
                token={token}
              />
            ))}
            {pendingOther && (
              <RoleChannelCard
                actions={<Button className="h-8" onClick={() => setPendingOther('')}>取消</Button>}
                description="新渠道。"
                id={pendingOther}
                key={pendingOther}
                main={main}
                nameEditable
                onChanged={resp => { apply(resp); setPendingOther(''); }}
                registered={registered}
                title={pendingOther}
                token={token}
              />
            )}
            {!pendingOther && (
              <div className="flex gap-2">
                <input
                  className={`${inputCls} max-w-xs`}
                  onChange={e => setNewOtherId(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') addOther(); }}
                  placeholder="新渠道 id，如 draw"
                  value={newOtherId}
                />
                <Button disabled={!newOtherId.trim()} onClick={addOther}>添加渠道</Button>
              </div>
            )}
          </section>
        </div>
      </section>
    </div>
  );
};
