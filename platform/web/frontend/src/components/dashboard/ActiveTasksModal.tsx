// [AutoC 2026-06-04] Active task monitor modal for the System dashboard.
// Why: operators need details behind the running-task count without changing the
// chat stream. How: poll the protected active-task summary endpoint every five
// seconds while the modal is open. Purpose: provide a small first-version monitor
// that can later be replaced by WebSocket updates without touching chatStore.
import { useEffect, useState } from 'react';

import { fetchActiveTasks, type ActiveTask } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import { Icon } from '../common';

interface ActiveTasksModalProps {
  open: boolean;
  onClose: () => void;
}

function shortId(value: string | null | undefined, length = 8): string {
  return value ? value.slice(0, length) : '';
}

function parseTime(value: string): number {
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : Date.now();
}

function formatRuntime(createdAt: string): string {
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - parseTime(createdAt)) / 1000));
  const minutes = Math.floor(elapsedSeconds / 60);
  const seconds = elapsedSeconds % 60;
  return `${minutes}m ${seconds}s`;
}

function formatRelative(updatedAt: string): string {
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - parseTime(updatedAt)) / 1000));
  if (elapsedSeconds < 60) return `${elapsedSeconds}s ago`;
  const minutes = Math.floor(elapsedSeconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function statusClass(status: ActiveTask['status']): string {
  if (status === 'running') return 'border-green-500/40 bg-green-500/10 text-green-700';
  if (status === 'pending') return 'border-yellow-500/40 bg-yellow-500/10 text-yellow-700';
  return 'border-gray-400/40 bg-gray-400/10 text-gray-600';
}

function taskTitle(task: ActiveTask): string {
  return task.node_id || shortId(task.task_id) || '未知任务';
}

export const ActiveTasksModal = ({ open, onClose }: ActiveTasksModalProps) => {
  const adminToken = useSettingsStore(state => state.adminToken);
  const [tasks, setTasks] = useState<ActiveTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;

    const load = async () => {
      if (!adminToken) {
        // [AutoC 2026-06-04] Why: the backend protects task summaries as an admin
        // endpoint. How: stop polling when no token is configured and show a clear
        // message. Purpose: avoid repeated 401 requests from an unauthenticated modal.
        if (!cancelled) {
          setTasks([]);
          setError('需要管理员令牌才能读取活跃任务。');
          setLoading(false);
        }
        return;
      }

      try {
        setLoading(true);
        const nextTasks = await fetchActiveTasks(adminToken);
        if (!cancelled) {
          setTasks(nextTasks);
          setError('');
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : '活跃任务刷新失败。');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    const timer = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [adminToken, open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onMouseDown={onClose}>
      <div
        aria-label="活跃任务详情"
        aria-modal="true"
        className="flex max-h-[86dvh] w-full max-w-3xl flex-col border border-[var(--duties-border)] bg-[var(--duties-panel)] shadow-xl"
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <div className="flex items-center justify-between border-b border-[var(--duties-border)] px-3 py-2">
          <div>
            <p className="font-mono text-[0.55rem] uppercase tracking-[0.18em] text-[var(--duties-tertiary)]">任务监控</p>
            <h2 className="font-mono text-sm font-semibold tracking-[-0.03em]">活跃任务详情</h2>
          </div>
          <button
            aria-label="关闭活跃任务详情"
            className="rounded-sm p-1 text-[var(--duties-tertiary)] transition-colors hover:bg-[var(--duties-muted)] hover:text-[var(--duties-text)]"
            onClick={onClose}
            type="button"
          >
            <Icon name="close" size={18} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {error && (
            <div className="mb-3 border border-orange-200 bg-orange-50 px-2.5 py-2 text-[0.65rem] text-orange-700">
              {error}
            </div>
          )}

          {loading && tasks.length === 0 ? (
            <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-4 text-center text-[0.75rem] text-[var(--duties-secondary)]">
              正在加载活跃任务…
            </div>
          ) : tasks.length === 0 ? (
            <div className="border border-[var(--duties-border)] bg-[var(--duties-bg)] px-3 py-4 text-center text-[0.75rem] text-[var(--duties-secondary)]">
              当前没有活跃任务
            </div>
          ) : (
            <ul className="space-y-2">
              {tasks.map(task => (
                <li
                  className="grid grid-cols-[minmax(0,1.4fr)_auto_minmax(5rem,0.7fr)_minmax(5rem,0.7fr)_minmax(4rem,0.6fr)] items-center gap-2 border border-[var(--duties-border)] bg-[var(--duties-bg)] p-2.5 text-[0.7rem]"
                  key={task.task_id}
                >
                  <div className="min-w-0">
                    <p className="truncate font-mono text-xs font-semibold text-[var(--duties-text)]">{taskTitle(task)}</p>
                    <p className="mt-0.5 truncate font-mono text-[0.6rem] text-[var(--duties-tertiary)]">{task.task_id}</p>
                  </div>
                  <span className={`rounded-sm border px-1.5 py-0.5 font-mono text-[0.6rem] ${statusClass(task.status)}`}>
                    {task.status}
                  </span>
                  <div className="font-mono text-[0.65rem] text-[var(--duties-secondary)]">
                    <p className="text-[0.55rem] uppercase tracking-[0.14em] text-[var(--duties-tertiary)]">运行时长</p>
                    <p>{formatRuntime(task.created_at)}</p>
                  </div>
                  <div className="font-mono text-[0.65rem] text-[var(--duties-secondary)]">
                    <p className="text-[0.55rem] uppercase tracking-[0.14em] text-[var(--duties-tertiary)]">最后活动</p>
                    <p>{formatRelative(task.updated_at)}</p>
                  </div>
                  <div className="min-w-0 font-mono text-[0.65rem] text-[var(--duties-secondary)]">
                    <p className="text-[0.55rem] uppercase tracking-[0.14em] text-[var(--duties-tertiary)]">worker</p>
                    <p className="truncate">{shortId(task.worker_id) || '无'}</p>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
};
