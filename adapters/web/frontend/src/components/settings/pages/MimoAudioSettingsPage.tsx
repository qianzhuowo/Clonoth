import { useEffect, useState } from 'react';

import { getMimoAudioConfig, updateMimoAudioConfig, type MimoAudioConfigPublic } from '../../../api/supervisorClient';
import { useSettingsStore } from '../../../store/settingsStore';
import { errorText, notify } from '../../../store/toastStore';
import { Button } from '../../common';

type FormState = {
  base_url: string;
  api_key: string;
  asr_path: string;
  asr_model: string;
  asr_language: string;
  tts_path: string;
  tts_model: string;
  tts_voice: string;
  tts_format: string;
  timeout: string;
  ffmpeg_path: string;
};

const defaults: FormState = {
  base_url: 'https://api.xiaomimimo.com',
  api_key: '',
  asr_path: '/v1/chat/completions',
  asr_model: 'mimo-v2.5-asr',
  asr_language: 'auto',
  tts_path: '/v1/chat/completions',
  tts_model: 'mimo-v2.5-tts',
  tts_voice: 'mimo_default',
  tts_format: 'wav',
  timeout: '60',
  ffmpeg_path: '',
};

const ttsVoiceOptions = [
  { value: 'mimo_default', label: 'MiMo 默认' },
  { value: '冰糖', label: '冰糖（中文女声）' },
  { value: '茉莉', label: '茉莉（中文女声）' },
  { value: '苏打', label: '苏打（中文男声）' },
  { value: '白桦', label: '白桦（中文男声）' },
  { value: 'Mia', label: 'Mia（英文女声）' },
  { value: 'Chloe', label: 'Chloe（英文女声）' },
  { value: 'Milo', label: 'Milo（英文男声）' },
  { value: 'Dean', label: 'Dean（英文男声）' },
];

function fromConfig(cfg: MimoAudioConfigPublic): FormState {
  return {
    base_url: cfg.base_url || defaults.base_url,
    api_key: '',
    asr_path: cfg.asr_path || defaults.asr_path,
    asr_model: cfg.asr_model || defaults.asr_model,
    asr_language: cfg.asr_language || defaults.asr_language,
    tts_path: cfg.tts_path || defaults.tts_path,
    tts_model: cfg.tts_model || defaults.tts_model,
    tts_voice: cfg.tts_voice || defaults.tts_voice,
    tts_format: cfg.tts_format || defaults.tts_format,
    timeout: cfg.timeout || defaults.timeout,
    ffmpeg_path: cfg.ffmpeg_path || defaults.ffmpeg_path,
  };
}

export const MimoAudioSettingsPage = () => {
  const { adminToken, isAuthenticated } = useSettingsStore();
  const [form, setForm] = useState<FormState>(defaults);
  const [redactedKey, setRedactedKey] = useState('');
  const [keyPresent, setKeyPresent] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const setField = (key: keyof FormState, value: string) => {
    setForm(prev => ({ ...prev, [key]: value }));
  };

  const load = async () => {
    if (!adminToken || !isAuthenticated) return;
    setLoading(true);
    try {
      const cfg = await getMimoAudioConfig(adminToken);
      setForm(fromConfig(cfg));
      setRedactedKey(cfg.api_key || '');
      setKeyPresent(cfg.api_key_present);
    } catch (err) {
      notify.error(errorText(err, '读取 Mimo 语音配置失败'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [adminToken, isAuthenticated]);

  const save = async () => {
    if (!adminToken) return;
    setSaving(true);
    try {
      const payload: Partial<FormState> = {
        base_url: form.base_url,
        asr_path: form.asr_path,
        asr_model: form.asr_model,
        asr_language: form.asr_language,
        tts_path: form.tts_path,
        tts_model: form.tts_model,
        tts_voice: form.tts_voice,
        tts_format: form.tts_format,
        timeout: form.timeout,
        ffmpeg_path: form.ffmpeg_path,
      };
      if (form.api_key.trim()) payload.api_key = form.api_key.trim();
      const cfg = await updateMimoAudioConfig(adminToken, payload);
      setForm(fromConfig(cfg));
      setRedactedKey(cfg.api_key || '');
      setKeyPresent(cfg.api_key_present);
      notify.success('已保存 Mimo 语音配置。');
    } catch (err) {
      notify.error(errorText(err, '保存 Mimo 语音配置失败'));
    } finally {
      setSaving(false);
    }
  };

  if (!isAuthenticated) {
    return (
      <section className="mx-auto flex h-full max-w-3xl flex-col overflow-y-auto p-4 sm:p-6">
        <div className="border border-[var(--duties-border)] p-4 text-sm text-[var(--duties-secondary)]">
          请先在“通用”页面登录管理员令牌，然后再配置 Mimo 语音服务。
        </div>
      </section>
    );
  }

  return (
    <section className="mx-auto flex h-full max-w-3xl flex-col overflow-y-auto p-4 sm:p-6">
      <div className="mb-5">
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">设置</p>
        <h2 className="mt-1 font-mono text-xl font-semibold tracking-[-0.04em]">语音</h2>
        <p className="mt-2 max-w-xl text-sm text-[var(--duties-secondary)]">
          配置 Mimo ASR 和 TTS。保存后会写入本机 data/config.yaml，后续启动会自动读取。
        </p>
      </div>

      <div className="space-y-4 border border-[var(--duties-border)] p-4">
        <div>
          <label className="mb-1 block text-xs text-[var(--duties-secondary)]">API Key</label>
          <input
            className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm outline-none focus:border-[var(--duties-text)]"
            onChange={e => setField('api_key', e.target.value)}
            placeholder={keyPresent ? `已保存：${redactedKey}，留空表示不修改` : '输入 Mimo API Key'}
            type="password"
            value={form.api_key}
          />
          <p className="mt-1 text-xs text-[var(--duties-tertiary)]">出于安全考虑，已保存的 Key 只显示脱敏结果。</p>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Base URL" value={form.base_url} onChange={v => setField('base_url', v)} />
          <Field label="超时秒数" value={form.timeout} onChange={v => setField('timeout', v)} />
          <Field label="ASR Path" value={form.asr_path} onChange={v => setField('asr_path', v)} />
          <Field label="ASR 模型" value={form.asr_model} onChange={v => setField('asr_model', v)} />
          <Field label="ASR 语言" value={form.asr_language} onChange={v => setField('asr_language', v)} />
          <Field label="TTS Path" value={form.tts_path} onChange={v => setField('tts_path', v)} />
          <Field label="TTS 模型" value={form.tts_model} onChange={v => setField('tts_model', v)} />
          <SelectField
            label="TTS 音色"
            options={ttsVoiceOptions}
            value={form.tts_voice}
            onChange={v => setField('tts_voice', v)}
          />
          <Field label="TTS 格式" value={form.tts_format} onChange={v => setField('tts_format', v)} />
          <Field label="ffmpeg 路径（可选）" value={form.ffmpeg_path} onChange={v => setField('ffmpeg_path', v)} />
        </div>

        <div className="flex gap-2">
          <Button disabled={saving || loading} onClick={save} variant="primary">
            {saving ? '保存中...' : '保存配置'}
          </Button>
          <Button disabled={loading || saving} onClick={() => void load()} variant="ghost">
            {loading ? '读取中...' : '重新读取'}
          </Button>
        </div>
      </div>
    </section>
  );
};

function Field(props: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs text-[var(--duties-secondary)]">{props.label}</span>
      <input
        className="w-full border border-[var(--duties-border)] bg-transparent px-3 py-2 font-mono text-sm outline-none focus:border-[var(--duties-text)]"
        onChange={e => props.onChange(e.target.value)}
        value={props.value}
      />
    </label>
  );
}

function SelectField(props: {
  label: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs text-[var(--duties-secondary)]">{props.label}</span>
      <select
        className="w-full border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-2 font-mono text-sm text-[var(--duties-text)] outline-none focus:border-[var(--duties-text)]"
        onChange={e => props.onChange(e.target.value)}
        value={props.value}
      >
        {props.options.map(option => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </label>
  );
}
