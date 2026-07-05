import { useEffect, useMemo, useState } from 'react';
import yaml from 'js-yaml';

import {
  cleanupDrawtoolsAttachments,
  getDrawtoolsBundle,
  getNodeRaw,
  initDrawtoolsConfigs,
  updateDrawtoolsCharactersRaw,
  updateDrawtoolsPromptRaw,
  updateDrawtoolsSettingsRaw,
  updateNodeRaw,
  type DrawtoolsBundle,
} from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { Button } from '../../common';
import { AuthRequired, Card, FieldLabel, PageHeader, PageShell, SelectInput, StatusText, TextInput } from './settingsPagePrimitives';

type SectionKey = 'settings' | 'characters' | 'node' | 'top-system' | 'output-format' | 'tag-guide';

const sectionLabels: Record<SectionKey, string> = {
  settings: '绘图设置 / 预设',
  characters: '角色标签库',
  node: '绘图分析节点 YAML',
  'top-system': 'Top System',
  'output-format': '输出格式',
  'tag-guide': 'Tag 指南',
};

const NAI_MODELS = [
  { value: 'nai-diffusion-4-5-full', label: 'nai-diffusion-4-5-full（V4.5 完整版，默认）' },
  { value: 'nai-diffusion-4-5-curated', label: 'nai-diffusion-4-5-curated（V4.5 策展版 / SFW）' },
  { value: 'nai-diffusion-4-full', label: 'nai-diffusion-4-full（V4 完整版）' },
  { value: 'nai-diffusion-4-curated', label: 'nai-diffusion-4-curated（V4 策展版）' },
  { value: 'nai-diffusion-3', label: 'nai-diffusion-3（V3 旧版）' },
];

const NAI_SAMPLERS = [
  'k_euler',
  'k_euler_ancestral',
  'k_dpmpp_2s_ancestral',
  'k_dpmpp_2m_sde',
  'k_dpmpp_2m',
  'k_dpmpp_sde',
];

const NAI_SCHEDULERS = ['karras', 'exponential', 'polyexponential'];

function parseYamlObject(text: string): any {
  try {
    const data = yaml.load(text);
    return data && typeof data === 'object' ? data : {};
  } catch {
    return {};
  }
}

function stringifyYaml(data: unknown): string {
  return yaml.dump(data, { lineWidth: 120, noRefs: true });
}

function getSectionContent(bundle: DrawtoolsBundle | null, key: SectionKey): string {
  if (!bundle) return '';
  if (key === 'settings') return bundle.settings?.content || '';
  if (key === 'characters') return bundle.character_tags?.content || '';
  if (key === 'node') return '';
  return bundle.prompts?.[key.replace('-', '_')]?.content || '';
}

function fileMeta(bundle: DrawtoolsBundle | null, key: SectionKey): string {
  if (!bundle) return '';
  if (key === 'node') return 'config/nodes/draw.novelai_planner.yaml';
  const item = key === 'settings'
    ? bundle.settings
    : key === 'characters'
      ? bundle.character_tags
      : bundle.prompts?.[key.replace('-', '_')];
  if (!item) return '';
  return item.exists ? `用户配置：${item.path}` : `正在显示模板：${item.example_path}`;
}

export const DrawtoolsSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const [bundle, setBundle] = useState<DrawtoolsBundle | null>(null);
  const [section, setSection] = useState<SectionKey>('settings');
  const [editor, setEditor] = useState('');
  const [nodeRaw, setNodeRaw] = useState('');
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const settingsObj = useMemo(() => parseYamlObject(bundle?.settings?.content || ''), [bundle]);
  const presets = Array.isArray(settingsObj?.params?.presets) ? settingsObj.params.presets : [];
  const selectedPresetId = String(settingsObj?.params?.selected_preset_id || '');
  const api = settingsObj?.api || {};
  const generation = settingsObj?.generation || {};
  const storage = settingsObj?.storage || {};
  const selectedPreset = presets.find((preset: any) => String(preset?.id || '') === selectedPresetId) || presets[0] || {};

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    try {
      const [data, nodeYaml] = await Promise.all([
        getDrawtoolsBundle(adminToken),
        getNodeRaw(adminToken, 'draw.novelai_planner').catch(() => ''),
      ]);
      setBundle(data);
      setNodeRaw(nodeYaml);
      setEditor(section === 'node' ? nodeYaml : getSectionContent(data, section));
      setMessage('');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '加载绘图配置失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [adminToken, isAuthenticated]);

  useEffect(() => {
    setEditor(section === 'node' ? nodeRaw : getSectionContent(bundle, section));
  }, [bundle, nodeRaw, section]);

  const saveCurrent = async () => {
    if (!adminToken) return;
    try {
      if (section === 'settings') await updateDrawtoolsSettingsRaw(adminToken, editor);
      else if (section === 'characters') await updateDrawtoolsCharactersRaw(adminToken, editor);
      else if (section === 'node') await updateNodeRaw(adminToken, 'draw.novelai_planner', editor);
      else await updateDrawtoolsPromptRaw(adminToken, section, editor);
      setMessage('已保存。用户配置文件会写入非 example 文件，后续更新不会覆盖。');
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存失败');
    }
  };

  const initConfigs = async () => {
    if (!adminToken) return;
    try {
      const result = await initDrawtoolsConfigs(adminToken);
      setMessage(result.created.length ? `已创建：${result.created.join('、')}` : '用户配置已存在，无需创建。');
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '初始化失败');
    }
  };

  const saveSettingsObject = async (next: any, okText: string) => {
    if (!adminToken) return;
    try {
      await updateDrawtoolsSettingsRaw(adminToken, stringifyYaml(next));
      setMessage(okText);
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存设置失败');
    }
  };

  const updateApiField = (key: string, value: string) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    next.api = { ...(next.api || {}), [key]: value };
    void saveSettingsObject(next, 'API 配置已保存。');
  };

  const updateGenerationField = (key: string, value: string) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    const numeric = Number(value);
    next.generation = { ...(next.generation || {}), [key]: Number.isFinite(numeric) ? numeric : value };
    void saveSettingsObject(next, '生成队列/重试配置已保存。');
  };

  const updateStorageField = (key: string, value: string | boolean) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    const numeric = typeof value === 'string' ? Number(value) : NaN;
    next.storage = { ...(next.storage || {}), [key]: typeof value === 'boolean' ? value : (Number.isFinite(numeric) ? numeric : value) };
    void saveSettingsObject(next, '附件清理配置已保存。');
  };

  const runCleanup = async () => {
    if (!adminToken) return;
    try {
      const result = await cleanupDrawtoolsAttachments(adminToken);
      setMessage(`清理完成：删除 ${result.deleted_count ?? 0} 个文件，释放 ${result.deleted_mb ?? 0} MB；剩余 ${result.remaining_count ?? 0} 个文件 / ${result.remaining_mb ?? 0} MB`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '清理失败');
    }
  };

  const switchPreset = (presetId: string) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    next.params = { ...(next.params || {}), selected_preset_id: presetId };
    void saveSettingsObject(next, `已切换默认预设：${presetId}`);
  };

  const updateSelectedPresetParam = (key: string, value: string | boolean) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    const list = Array.isArray(next?.params?.presets) ? next.params.presets : [];
    const idx = list.findIndex((preset: any) => String(preset?.id || '') === selectedPresetId);
    if (idx < 0) { setMessage('请先选择一个预设'); return; }
    const numeric = typeof value === 'string' ? Number(value) : NaN;
    list[idx] = {
      ...list[idx],
      params: {
        ...(list[idx].params || {}),
        [key]: typeof value === 'boolean' ? value : (Number.isFinite(numeric) && value !== '' ? numeric : value),
      },
    };
    next.params = { ...(next.params || {}), presets: list };
    void saveSettingsObject(next, `已保存预设参数：${key}`);
  };

  const updateSelectedPresetField = (key: string, value: string) => {
    const next = parseYamlObject(bundle?.settings?.content || '');
    const list = Array.isArray(next?.params?.presets) ? next.params.presets : [];
    const idx = list.findIndex((preset: any) => String(preset?.id || '') === selectedPresetId);
    if (idx < 0) { setMessage('请先选择一个预设'); return; }
    list[idx] = { ...list[idx], [key]: value };
    next.params = { ...(next.params || {}), presets: list };
    void saveSettingsObject(next, `已保存预设字段：${key}`);
  };

  return (
    <PageShell>
      <PageHeader
        description="管理 NovelAI 绘图配置、画师串预设、角色标签库和提示词模板。默认只维护 .example，点击初始化后才创建用户配置文件。"
        title="NovelAI 绘图"
      />
      {!isAuthenticated ? <AuthRequired /> : (
        <>
          <Card title="运行期配置" description="用户配置写入 settings.yaml / character_tags.yaml / prompt .md；仓库更新只维护 .example。">
            <div className="flex flex-wrap gap-2">
              <Button disabled={loading} onClick={load}>{loading ? '加载中...' : '刷新'}</Button>
              <Button onClick={initConfigs} variant="primary">从 Example 初始化用户配置</Button>
            </div>
            <StatusText message={message} />
          </Card>

          <Card title="API 配置" description="API Key 推荐使用环境变量 NOVELAI_API_KEY；如必须写入文件，可在下方保存。">
            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <FieldLabel>NovelAI / 兼容站点 Base URL</FieldLabel>
                <TextInput defaultValue={String(api.base_url || '')} onBlur={(event) => updateApiField('base_url', event.currentTarget.value)} placeholder="https://image.novelai.net" />
              </div>
              <div>
                <FieldLabel>API Key 环境变量名</FieldLabel>
                <TextInput defaultValue={String(api.api_key_env || 'NOVELAI_API_KEY')} onBlur={(event) => updateApiField('api_key_env', event.currentTarget.value)} />
              </div>
              <div className="md:col-span-2">
                <FieldLabel>API Key（可选，不推荐提交到仓库）</FieldLabel>
                <TextInput defaultValue={String(api.api_key || '')} onBlur={(event) => updateApiField('api_key', event.currentTarget.value)} placeholder="留空则使用环境变量" type="password" />
              </div>
            </div>
          </Card>

          <Card title="生成队列 / 重试" description="NovelAI 请求会通过全局队列串行执行；429/5xx/连接失败会自动重试。">
            <div className="grid gap-3 md:grid-cols-3">
              <div>
                <FieldLabel>生成间隔（秒）</FieldLabel>
                <TextInput defaultValue={String(generation.request_delay_sec ?? 0)} onBlur={(event) => updateGenerationField('request_delay_sec', event.currentTarget.value)} type="number" />
              </div>
              <div>
                <FieldLabel>重试等待（秒）</FieldLabel>
                <TextInput defaultValue={String(generation.retry_wait_sec ?? 3)} onBlur={(event) => updateGenerationField('retry_wait_sec', event.currentTarget.value)} type="number" />
              </div>
              <div>
                <FieldLabel>重试次数</FieldLabel>
                <TextInput defaultValue={String(generation.retry_max_attempts ?? 5)} onBlur={(event) => updateGenerationField('retry_max_attempts', event.currentTarget.value)} type="number" />
              </div>
            </div>
          </Card>

          <Card title="附件清理" description="自动清理 data/attachments/novelai：先删除过期文件，若仍超过容量上限再删除最旧文件。">
            <div className="grid gap-3 md:grid-cols-4">
              <label className="flex items-center gap-2 text-xs text-[var(--duties-secondary)]">
                <input checked={Boolean(storage.cleanup_enabled ?? true)} onChange={(event) => updateStorageField('cleanup_enabled', event.currentTarget.checked)} type="checkbox" />
                启用自动清理
              </label>
              <div>
                <FieldLabel>保留天数</FieldLabel>
                <TextInput defaultValue={String(storage.retention_days ?? 7)} onBlur={(event) => updateStorageField('retention_days', event.currentTarget.value)} type="number" />
              </div>
              <div>
                <FieldLabel>最大容量 MB</FieldLabel>
                <TextInput defaultValue={String(storage.max_total_mb ?? 2048)} onBlur={(event) => updateStorageField('max_total_mb', event.currentTarget.value)} type="number" />
              </div>
              <div>
                <FieldLabel>清理检查间隔 秒</FieldLabel>
                <TextInput defaultValue={String(storage.cleanup_interval_sec ?? 3600)} onBlur={(event) => updateStorageField('cleanup_interval_sec', event.currentTarget.value)} type="number" />
              </div>
            </div>
            <Button className="mt-3" onClick={runCleanup}>立即清理</Button>
          </Card>

          <Card title="画师串 / 预设" description="绘图节点只看见预设名称，具体参数由工具层自动拼接。">
            <div className="mb-3 max-w-xs">
              <FieldLabel>当前默认预设</FieldLabel>
              <SelectInput onChange={(event) => switchPreset(event.currentTarget.value)} value={selectedPresetId}>
                {presets.map((preset: any) => <option key={preset.id} value={preset.id}>{preset.name || preset.id}</option>)}
              </SelectInput>
            </div>
            {selectedPreset?.id && (
              <div className="mb-4 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3">
                <h3 className="mb-3 font-mono text-xs font-semibold">当前预设快捷编辑：{selectedPreset.name || selectedPreset.id}</h3>
                <div className="grid gap-3 md:grid-cols-3">
                  <div>
                    <FieldLabel>绘图模型</FieldLabel>
                    <SelectInput onChange={(event) => updateSelectedPresetParam('model', event.currentTarget.value)} value={String(selectedPreset.params?.model || 'nai-diffusion-4-5-full')}>
                      {NAI_MODELS.map((model) => <option key={model.value} value={model.value}>{model.label}</option>)}
                    </SelectInput>
                  </div>
                  <div>
                    <FieldLabel>采样器</FieldLabel>
                    <SelectInput onChange={(event) => updateSelectedPresetParam('sampler', event.currentTarget.value)} value={String(selectedPreset.params?.sampler || 'k_euler_ancestral')}>
                      {NAI_SAMPLERS.map((sampler) => <option key={sampler} value={sampler}>{sampler}</option>)}
                    </SelectInput>
                  </div>
                  <div>
                    <FieldLabel>噪声调度</FieldLabel>
                    <SelectInput onChange={(event) => updateSelectedPresetParam('scheduler', event.currentTarget.value)} value={String(selectedPreset.params?.scheduler || 'karras')}>
                      {NAI_SCHEDULERS.map((scheduler) => <option key={scheduler} value={scheduler}>{scheduler}</option>)}
                    </SelectInput>
                  </div>
                  <div>
                    <FieldLabel>Steps</FieldLabel>
                    <TextInput defaultValue={String(selectedPreset.params?.steps ?? 28)} onBlur={(event) => updateSelectedPresetParam('steps', event.currentTarget.value)} type="number" />
                  </div>
                  <div>
                    <FieldLabel>CFG / Scale</FieldLabel>
                    <TextInput defaultValue={String(selectedPreset.params?.scale ?? 6)} onBlur={(event) => updateSelectedPresetParam('scale', event.currentTarget.value)} type="number" />
                  </div>
                  <div>
                    <FieldLabel>负面预设</FieldLabel>
                    <SelectInput onChange={(event) => updateSelectedPresetParam('ucPreset', event.currentTarget.value)} value={String(selectedPreset.params?.ucPreset ?? 0)}>
                      <option value="0">Heavy</option>
                      <option value="1">Light</option>
                      <option value="2">Human Focus</option>
                      <option value="3">None</option>
                    </SelectInput>
                  </div>
                  <label className="flex items-center gap-2 text-xs text-[var(--duties-secondary)]">
                    <input checked={Boolean(selectedPreset.params?.qualityToggle ?? true)} onChange={(event) => updateSelectedPresetParam('qualityToggle', event.currentTarget.checked)} type="checkbox" />
                    质量增强
                  </label>
                  <label className="flex items-center gap-2 text-xs text-[var(--duties-secondary)]">
                    <input checked={Boolean(selectedPreset.params?.autoSmea ?? false)} onChange={(event) => updateSelectedPresetParam('autoSmea', event.currentTarget.checked)} type="checkbox" />
                    自动 SMEA
                  </label>
                  <label className="flex items-center gap-2 text-xs text-[var(--duties-secondary)]">
                    <input checked={Boolean(selectedPreset.params?.variety_boost ?? false)} onChange={(event) => updateSelectedPresetParam('variety_boost', event.currentTarget.checked)} type="checkbox" />
                    多样性增强 (V4.5)
                  </label>
                  <div>
                    <FieldLabel>CFG 重缩放</FieldLabel>
                    <TextInput defaultValue={String(selectedPreset.params?.cfg_rescale ?? 0)} onBlur={(event) => updateSelectedPresetParam('cfg_rescale', event.currentTarget.value)} step="0.05" type="number" />
                  </div>
                  <div className="md:col-span-3">
                    <FieldLabel>正面前缀</FieldLabel>
                    <TextInput defaultValue={String(selectedPreset.positive_prefix || '')} onBlur={(event) => updateSelectedPresetField('positive_prefix', event.currentTarget.value)} />
                  </div>
                  <div className="md:col-span-3">
                    <FieldLabel>负面前缀</FieldLabel>
                    <TextInput defaultValue={String(selectedPreset.negative_prefix || '')} onBlur={(event) => updateSelectedPresetField('negative_prefix', event.currentTarget.value)} />
                  </div>
                </div>
              </div>
            )}
            <div className="space-y-2">
              {presets.map((preset: any) => (
                <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3" key={preset.id}>
                  <div className="font-mono text-xs font-semibold">{preset.name || preset.id} {preset.id === selectedPresetId ? '· 当前默认' : ''}</div>
                  <p className="mt-1 text-xs text-[var(--duties-secondary)]">id: {preset.id} · model: {preset.params?.model} · sampler: {preset.params?.sampler} · scheduler: {preset.params?.scheduler} · CFG: {preset.params?.scale} · steps: {preset.params?.steps}</p>
                </div>
              ))}
            </div>
          </Card>

          <Card title="原始配置编辑器" description={fileMeta(bundle, section)}>
            <div className="mb-3 grid gap-2 md:grid-cols-2">
              <FieldLabel>编辑对象</FieldLabel>
              <SelectInput onChange={(event) => setSection(event.currentTarget.value as SectionKey)} value={section}>
                {(Object.keys(sectionLabels) as SectionKey[]).map((key) => <option key={key} value={key}>{sectionLabels[key]}</option>)}
              </SelectInput>
            </div>
            <textarea
              className="h-[32rem] w-full resize-y border border-[var(--duties-border)] bg-[var(--duties-bg)] p-3 font-mono text-xs leading-5 text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
              onChange={(event) => setEditor(event.currentTarget.value)}
              spellCheck={false}
              value={editor}
            />
            <div className="mt-3 flex flex-wrap gap-2">
              <Button onClick={saveCurrent} variant="primary">保存当前文件</Button>
              <Button onClick={() => setEditor(section === 'node' ? nodeRaw : getSectionContent(bundle, section))}>撤销未保存修改</Button>
            </div>
          </Card>
        </>
      )}
    </PageShell>
  );
};
