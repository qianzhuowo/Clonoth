import { useEffect, useState, type ReactNode } from 'react';

import * as supervisorClient from '../../../api/supervisorClient';
import type {
  UsageByModelRow, UsageByNodeRow, UsageByProviderRow, UsageModelPricing,
  UsagePricingResponse, UsageRange, UsageStatsResponse, UsageTimeseriesPoint,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { errorText, notify } from '../../../store/toastStore';
import { Modal } from '../../common/Modal';
import { AuthRequired, Card, FieldLabel, PageHeader, TextInput } from './settingsPagePrimitives';

const number = (value: number) => value.toLocaleString();
const money = (value: number, digits = 4) => `$${value.toFixed(digits)}`;
const percent = (value: number) => `${(value * 100).toFixed(1)}%`;
const buttonClass = 'border border-[var(--duties-border)] px-3 py-2 font-mono text-xs transition-colors hover:bg-[var(--duties-muted)] disabled:cursor-wait disabled:opacity-50';
const cellClass = 'border-b border-[var(--duties-border)] px-3 py-3 text-left align-top';
const ranges: [UsageRange, string][] = [['today', '今日'], ['7d', '近 7 天'], ['30d', '近 30 天'], ['all', '全部']];
const priceFields = [['input', '输入'], ['output', '输出'], ['cache_read', '缓存命中'], ['cache_write', '缓存写入']] as const;
const emptyPrice: UsageModelPricing = { input: 0, output: 0, cache_read: 0, cache_write: 0 };
const priceDraft = (price: UsageModelPricing) => ({
  input: String(price.input), output: String(price.output),
  cache_read: String(price.cache_read), cache_write: String(price.cache_write),
});

function Progress({ value }: { value: number }) {
  return <div className="mt-2 h-1.5 w-full bg-[var(--duties-muted)]"><div className="h-full bg-emerald-500" style={{ width: `${Math.min(100, Math.max(0, value * 100))}%` }} /></div>;
}

function Kpi({ label, value, children, accent = false }: { label: string; value: string; children: ReactNode; accent?: boolean }) {
  return <Card>
    <p className="font-mono text-xs text-[var(--duties-secondary)]">{label}</p>
    <p className={`mt-3 break-all font-mono text-2xl font-semibold tracking-tight ${accent ? 'text-emerald-500' : ''}`}>{value}</p>
    <div className="mt-3 space-y-1 font-mono text-[0.65rem] leading-5 text-[var(--duties-secondary)]">{children}</div>
  </Card>;
}

function UsageTrend({ points }: { points: UsageTimeseriesPoint[] }) {
  const [activeBucket, setActiveBucket] = useState<string | null>(null);
  if (points.length === 0) return <p className="py-12 text-center text-xs text-[var(--duties-secondary)]">所选时间范围暂无用量。</p>;
  const selected = points.find(point => point.bucket === activeBucket) ?? points[points.length - 1];
  const maxTokens = Math.max(1, ...points.map(point => point.prompt_tokens + point.completion_tokens));
  const maxCost = Math.max(...points.map(point => point.cost_usd));
  const width = Math.max(640, points.length * 28);
  const chartHeight = 164;
  const base = 184;
  const step = (width - 60) / points.length;
  const x = (index: number) => 40 + step * index + step / 2;
  const costY = (cost: number) => base - (maxCost > 0 ? cost / maxCost * chartHeight : 0);
  return <>
    <div className="mb-3 flex flex-wrap gap-4 font-mono text-[0.65rem] text-[var(--duties-secondary)]">
      <span><span className="mr-1 inline-block h-2 w-2 bg-sky-500" />未缓存输入</span>
      <span><span className="mr-1 inline-block h-2 w-2 bg-emerald-500" />缓存命中</span>
      <span><span className="mr-1 inline-block h-2 w-2 bg-violet-500" />输出（含思考）</span>
      <span><span className="mr-1 inline-block h-0.5 w-3 bg-amber-500 align-middle" />费用 USD · 峰值 {money(maxCost)}</span>
    </div>
    <div className="overflow-x-auto">
      <svg aria-label="Token 堆叠柱状图与费用趋势线" role="img" viewBox={`0 0 ${width} 220`} className="h-56 w-full" style={{ minWidth: width }}>
        {[0, 0.5, 1].map(ratio => <g key={ratio}>
          <line x1="40" x2={width} y1={base - ratio * chartHeight} y2={base - ratio * chartHeight} stroke="var(--duties-border)" strokeDasharray="3 4" />
          <text x="0" y={base - ratio * chartHeight + 4} fill="var(--duties-tertiary)" fontSize="9" fontFamily="monospace">{Intl.NumberFormat('en', { notation: 'compact' }).format(maxTokens * ratio)}</text>
        </g>)}
        {points.map((point, index) => {
          const input = Math.max(0, point.prompt_tokens - point.cached_prompt_tokens) / maxTokens * chartHeight;
          const cache = point.cached_prompt_tokens / maxTokens * chartHeight;
          const output = point.completion_tokens / maxTokens * chartHeight;
          const barWidth = step * 0.62;
          return <g key={point.bucket} tabIndex={0} role="button" aria-label={`${point.bucket}，${point.request_count} 次请求，${money(point.cost_usd)}`} onFocus={() => setActiveBucket(point.bucket)} onMouseEnter={() => setActiveBucket(point.bucket)} onClick={() => setActiveBucket(point.bucket)} className="cursor-pointer outline-none">
            <title>{`${point.bucket}\n请求 ${number(point.request_count)} · 输入 ${number(point.prompt_tokens)} · 缓存 ${number(point.cached_prompt_tokens)} · 输出 ${number(point.completion_tokens)}\n费用 ${money(point.cost_usd, 6)}`}</title>
            <rect x={x(index) - step / 2} y="16" width={step} height="174" fill={selected.bucket === point.bucket ? 'var(--duties-muted)' : 'transparent'} />
            <rect x={x(index) - barWidth / 2} y={base - input} width={barWidth} height={input} fill="#0ea5e9" />
            <rect x={x(index) - barWidth / 2} y={base - input - cache} width={barWidth} height={cache} fill="#10b981" />
            <rect x={x(index) - barWidth / 2} y={base - input - cache - output} width={barWidth} height={output} fill="#8b5cf6" />
            {(index % Math.ceil(points.length / 8) === 0 || index === points.length - 1) && <text x={x(index)} y="207" textAnchor="middle" fill="var(--duties-secondary)" fontSize="9" fontFamily="monospace">{point.bucket}</text>}
          </g>;
        })}
        <polyline points={points.map((point, index) => `${x(index)},${costY(point.cost_usd)}`).join(' ')} fill="none" stroke="#f59e0b" strokeWidth="2" pointerEvents="none" />
      </svg>
    </div>
    <div aria-live="polite" className="mt-2 flex flex-wrap gap-x-5 gap-y-1 border-t border-[var(--duties-border)] pt-3 font-mono text-xs text-[var(--duties-secondary)]">
      <span>{selected.bucket}</span><span>{number(selected.request_count)} 次请求</span><span>In {number(selected.prompt_tokens)}</span><span>Cache {number(selected.cached_prompt_tokens)}</span><span>Out {number(selected.completion_tokens)}</span><span className="text-[var(--duties-text)]">{money(selected.cost_usd, 6)}</span>
    </div>
  </>;
}

type Distribution = 'model' | 'node' | 'provider';
function DistributionTable({ data, onEdit }: { data: UsageStatsResponse; onEdit: (model: string) => void }) {
  const [dimension, setDimension] = useState<Distribution>('model');
  const rows: (UsageByModelRow | UsageByNodeRow | UsageByProviderRow)[] = dimension === 'model' ? data.by_model : dimension === 'node' ? data.by_node : data.by_provider;
  return <Card title="多维用量分布">
    <div className="mb-3 flex flex-wrap gap-1" role="group" aria-label="统计维度">
      {([['model', '按模型统计'], ['node', '按 Agent 节点'], ['provider', '按 Provider 渠道']] as const).map(([key, label]) => <button type="button" key={key} aria-pressed={dimension === key} onClick={() => setDimension(key)} className={`${buttonClass} ${dimension === key ? 'bg-[var(--duties-muted)] text-[var(--duties-text)]' : 'text-[var(--duties-secondary)]'}`}>{label}</button>)}
    </div>
    <div className="overflow-x-auto">
      <table className="w-full whitespace-nowrap font-mono text-xs">
        <thead className="text-[var(--duties-tertiary)]"><tr>{[dimension === 'model' ? '模型 / 生效单价 ($/1M)' : dimension === 'node' ? 'Agent 节点' : 'Provider 渠道', '请求', 'Input', 'Output', 'Cache Read', 'Reasoning', '缓存命中', ...(dimension === 'model' ? ['平均速率'] : []), '累计费用'].map(title => <th key={title} className={cellClass}>{title}</th>)}</tr></thead>
        <tbody>{rows.map(row => {
          const modelRow = 'model' in row ? row : null;
          const key = modelRow ? `${modelRow.provider}/${modelRow.model}` : 'node_id' in row ? row.node_id : row.provider;
          return <tr key={key} className="hover:bg-[var(--duties-muted)]">
            <td className={cellClass}>{modelRow ? <>
              <div className="flex items-center gap-2"><button type="button" className="text-left hover:underline" onClick={() => onEdit(modelRow.model)}>{modelRow.model}</button><span className="border border-[var(--duties-border)] px-1.5 py-0.5 text-[0.6rem] text-[var(--duties-secondary)]">{modelRow.provider}</span></div>
              <button type="button" onClick={() => onEdit(modelRow.model)} className="mt-2 text-[0.6rem] text-[var(--duties-secondary)] hover:text-[var(--duties-text)] hover:underline">
                {modelRow.pricing ? `${modelRow.pricing.is_override ? '自定义' : '官方默认'} · In $${modelRow.pricing.input} / Out $${modelRow.pricing.output} / Read $${modelRow.pricing.cache_read} / Write $${modelRow.pricing.cache_write}` : '未配置定价 · 点击设置'}
              </button>
            </> : key}</td>
            <td className={cellClass}>{number(row.request_count)}</td><td className={cellClass}>{number(row.prompt_tokens)}</td><td className={cellClass}>{number(row.completion_tokens)}</td><td className={`${cellClass} text-emerald-500`}>{number(row.cached_prompt_tokens)}</td><td className={cellClass}>{number(row.reasoning_tokens)}</td>
            <td className={`${cellClass} min-w-28`}>{percent(row.cache_hit_rate)}<Progress value={row.cache_hit_rate} /></td>
            {modelRow && <td className={cellClass}>{modelRow.avg_tps.toFixed(1)} tok/s</td>}
            <td className={`${cellClass} font-semibold`}>{money(row.cost_usd)}</td>
          </tr>;
        })}</tbody>
      </table>
      {rows.length === 0 && <p className="py-8 text-center text-xs text-[var(--duties-secondary)]">暂无用量记录。</p>}
    </div>
  </Card>;
}

function PricingManager({ pricing, models, initialModel, onClose, onSaved }: {
  pricing: UsagePricingResponse; models: UsageByModelRow[]; initialModel: string; onClose: () => void; onSaved: () => void;
}) {
  const resolvePrice = (name: string, table: UsagePricingResponse = pricing) => {
    if (table.effective[name]) return table.effective[name];
    const rowPrice = models.find(row => row.model === name)?.pricing;
    if (rowPrice) return rowPrice;
    const candidates = [name.toLowerCase(), name.split('/').pop()!.toLowerCase()];
    const matched = Object.keys(table.effective)
      .sort((a, b) => b.length - a.length)
      .find(k => candidates.some(c => c.startsWith(k.toLowerCase())));
    return matched ? table.effective[matched] : emptyPrice;
  };
  const [model, setModel] = useState(initialModel);
  const [draft, setDraft] = useState(() => priceDraft(resolvePrice(initialModel)));
  const [saving, setSaving] = useState(false);
  const names = [...new Set([...Object.keys(pricing.defaults), ...Object.keys(pricing.effective), ...Object.keys(pricing.overrides), ...models.map(row => row.model)])].sort();
  const select = (name: string) => { setModel(name); setDraft(priceDraft(resolvePrice(name))); };
  const save = async (reset: boolean) => {
    setSaving(true);
    try {
      const nextPricing = await supervisorClient.updateModelPricing(model.trim(), reset ? null : { input: Number(draft.input), output: Number(draft.output), cache_read: Number(draft.cache_read), cache_write: Number(draft.cache_write) });
      if (reset) setDraft(priceDraft(resolvePrice(model.trim(), nextPricing)));
      notify.success(reset ? '已恢复官方默认，历史费用将按默认价重算。' : '定价已保存，历史费用已按新价格重算。');
      onSaved();
    } catch (error) {
      notify.error(errorText(error, '保存定价失败'));
    } finally {
      setSaving(false);
    }
  };
  return <Modal open onClose={onClose} title="管理模型定价" subtitle="USD / 1M TOKENS" maxWidth="max-w-5xl">
    <div className="space-y-5 p-4">
      <p className="text-xs leading-5 text-[var(--duties-secondary)]">自定义单价覆盖官方默认值，保存后立即重算历史账单。这里展示的是预估费用，实际结算以渠道账单为准。</p>
      <form className="grid gap-3 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-4 sm:grid-cols-2 lg:grid-cols-4" onSubmit={event => { event.preventDefault(); void save(false); }}>
        <div className="sm:col-span-2 lg:col-span-4"><FieldLabel htmlFor="usage-price-model">模型名称（选择下表或输入新模型）</FieldLabel><TextInput id="usage-price-model" required value={model} disabled={saving} onChange={event => select(event.target.value)} placeholder="例如 gpt-4.1" /></div>
        {priceFields.map(([key, label]) => <div key={key}><FieldLabel htmlFor={`usage-price-${key}`}>{label} · $/1M Tokens</FieldLabel><TextInput id={`usage-price-${key}`} type="number" min="0" step="any" required disabled={saving} value={draft[key]} onChange={event => setDraft(current => ({ ...current, [key]: event.target.value }))} /></div>)}
        <div className="flex flex-wrap items-center gap-2 sm:col-span-2 lg:col-span-4">
          <button type="submit" disabled={saving || !model.trim()} className={`${buttonClass} bg-[var(--duties-muted)]`}>{saving ? '保存中…' : '保存自定义定价'}</button>
          {pricing.overrides[model] && <button type="button" disabled={saving} className={buttonClass} onClick={() => void save(true)}>恢复官方默认</button>}
        </div>
      </form>
      <div className="overflow-x-auto"><table className="w-full whitespace-nowrap font-mono text-xs">
        <thead className="text-[var(--duties-tertiary)]"><tr>{['模型', '状态', '官方默认 In / Out / Read / Write', '当前生效 In / Out / Read / Write'].map(title => <th key={title} className={cellClass}>{title}</th>)}</tr></thead>
        <tbody>{names.map(name => {
          const official = pricing.defaults[name];
          const effective = resolvePrice(name);
          return <tr key={name} className={model === name ? 'bg-[var(--duties-muted)]' : ''}>
            <td className={cellClass}><button type="button" disabled={saving} className="hover:underline" onClick={() => select(name)}>{name}</button></td>
            <td className={cellClass}>{pricing.overrides[name] ? '自定义' : official ? '官方默认' : effective.matched_key ? `匹配 ${effective.matched_key}` : '未配置'}</td>
            <td className={`${cellClass} text-[var(--duties-secondary)]`}>{official ? priceFields.map(([key]) => `$${official[key]}`).join(' / ') : '—'}</td>
            <td className={cellClass}>{priceFields.map(([key]) => `$${effective[key]}`).join(' / ')}</td>
          </tr>;
        })}</tbody>
      </table></div>
    </div>
  </Modal>;
}

export function UsageSettingsPage() {
  const adminToken = useSettingsStore(state => state.adminToken);
  const [range, setRange] = useState<UsageRange>('7d');
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<UsageStatsResponse | null>(null);
  const [pricing, setPricing] = useState<UsagePricingResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const refresh = () => setRevision(current => current + 1);

  useEffect(() => {
    if (!adminToken) return;
    let cancelled = false;
    setLoading(true);
    setError('');
    Promise.all([supervisorClient.getUsageStats(range), supervisorClient.getUsagePricing()])
      .then(([stats, prices]) => { if (!cancelled) { setData(stats); setPricing(prices); } })
      .catch((cause: unknown) => {
        if (cancelled) return;
        const text = errorText(cause, '未知错误');
        // Keep the persistent alert (with retry) and also surface a toast.
        setError(text);
        notify.error(`用量加载失败：${text}`);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [adminToken, range, revision]);

  const summary = data?.summary;
  return <section className="h-full min-h-0 overflow-y-auto p-4 sm:p-6"><div className="mx-auto max-w-6xl space-y-5">
    <div className="flex flex-wrap items-end justify-between gap-4">
      <PageHeader eyebrow="USAGE / BILLING" title="用量与计费统计" description="追踪 Token、Prompt 缓存与模型成本。费用按当前模型定价估算，时间范围使用浏览器本地时区。" />
      <div className="flex flex-wrap gap-2">
        <div className="flex" role="group" aria-label="时间范围">{ranges.map(([value, label]) => <button type="button" key={value} aria-pressed={range === value} className={`${buttonClass} ${range === value ? 'bg-[var(--duties-muted)] font-semibold' : 'text-[var(--duties-secondary)]'}`} onClick={() => setRange(value)}>{label}</button>)}</div>
        <button type="button" className={buttonClass} disabled={!pricing || !adminToken} onClick={() => setEditingModel('')}>管理模型定价</button>
        <button type="button" className={buttonClass} disabled={loading || !adminToken} onClick={refresh}>{loading && adminToken ? '刷新中…' : '刷新'}</button>
      </div>
    </div>
    {!adminToken ? <AuthRequired /> : <>
      {error && <p role="alert" className="border border-red-500/40 p-3 text-sm text-red-500">用量加载失败：{error}<button type="button" className="ml-3 underline" onClick={refresh}>重试</button></p>}
      {loading && <p role="status" className="font-mono text-xs text-[var(--duties-tertiary)]">正在加载用量与定价…</p>}
      {data && summary && <div className={`space-y-5 ${loading ? 'opacity-50' : ''}`} aria-busy={loading}>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Kpi label="预估总花费 · USD" value={money(summary.total_cost_usd)} accent><p>平均单次 {money(summary.request_count ? summary.total_cost_usd / summary.request_count : 0, 6)}</p><p>按当前生效单价计算</p></Kpi>
          <Kpi label="总 Token 用量" value={number(summary.total_tokens)}><p>输入 {number(summary.prompt_tokens)} / 输出 {number(summary.completion_tokens)}</p><p>思考 {number(summary.reasoning_tokens)}（输出内）</p></Kpi>
          <Kpi label="Prompt 缓存命中率" value={percent(summary.cache_hit_rate)}><p>命中 {number(summary.cached_prompt_tokens)}</p><p>缓存写入 {number(summary.cache_write_prompt_tokens)}</p><Progress value={summary.cache_hit_rate} /></Kpi>
          <Kpi label="请求数与吞吐性能" value={`${number(summary.request_count)} 次`}><p>平均耗时 {(summary.avg_elapsed_ms / 1000).toFixed(2)} s</p><p>平均生成速率 {summary.avg_tps.toFixed(1)} tok/s</p></Kpi>
        </div>
        <Card title="Token 与费用趋势" description="悬停、点击或聚焦时间桶查看请求数与费用；柱状图对应左轴 Token，折线独立按费用峰值缩放。"><UsageTrend points={data.timeseries} /></Card>
        <DistributionTable data={data} onEdit={setEditingModel} />
        <Card title="最近请求明细" description="逐次记录 Token 构成、生成性能和预估费用。Cache 是输入 Token 的子集，Think 是输出 Token 的子集。">
          <div className="overflow-x-auto"><table className="w-full whitespace-nowrap font-mono text-xs">
            <thead className="text-[var(--duties-tertiary)]"><tr>{['时间', 'Agent 节点', 'Provider / Model', 'Token 构成', '耗时 / 生成速率', '单次花费'].map(title => <th key={title} className={cellClass}>{title}</th>)}</tr></thead>
            <tbody>{data.recent.map(row => <tr key={row.id} className="hover:bg-[var(--duties-muted)]">
              <td className={cellClass}><time dateTime={row.created_at}>{new Date(row.ts * 1000).toLocaleString()}</time></td>
              <td className={cellClass}>{row.node_id}{row.is_system_task && <span className="ml-2 border border-[var(--duties-border)] px-1 text-[0.6rem] text-amber-500">SYS</span>}</td>
              <td className={cellClass}><p>{row.model}</p><p className="mt-1 text-[0.65rem] text-[var(--duties-secondary)]">{row.provider}</p></td>
              <td className={cellClass}><p>In {number(row.prompt_tokens)} / Out {number(row.completion_tokens)}</p><p className="mt-1 text-emerald-500">Cache {number(row.cached_prompt_tokens)} <span className="text-violet-500">/ Think {number(row.reasoning_tokens)}</span></p><p className="mt-1 text-[0.6rem] text-[var(--duties-tertiary)]">Write {number(row.cache_write_prompt_tokens)}</p></td>
              <td className={`${cellClass} min-w-40`}><p>{(row.elapsed_ms / 1000).toFixed(2)} s <span className="text-emerald-500">/ {row.tps.toFixed(1)} tok/s</span></p><div className="mt-2 flex gap-1" title="耗时条以 60 秒为满格，速率条以 100 tok/s 为满格"><div className="h-1 flex-1 bg-[var(--duties-muted)]"><div className="h-full bg-amber-500" style={{ width: `${Math.min(row.elapsed_ms / 60000, 1) * 100}%` }} /></div><div className="h-1 flex-1 bg-[var(--duties-muted)]"><div className="h-full bg-emerald-500" style={{ width: `${Math.min(row.tps / 100, 1) * 100}%` }} /></div></div></td>
              <td className={`${cellClass} font-semibold`}>{money(row.cost_usd, 6)}</td>
            </tr>)}</tbody>
          </table>{data.recent.length === 0 && <p className="py-8 text-center text-xs text-[var(--duties-secondary)]">暂无请求明细。</p>}</div>
        </Card>
      </div>}
      {editingModel !== null && pricing && data && <PricingManager key={editingModel} pricing={pricing} models={data.by_model} initialModel={editingModel} onClose={() => setEditingModel(null)} onSaved={refresh} />}
    </>}
  </div></section>;
}
