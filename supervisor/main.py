from __future__ import annotations

import argparse
import logging
import socket
import atexit
import io
import signal
import threading
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .api import create_app
from .config_store import ConfigStore
from .eventlog import EventLog
from .policy import PolicyEngine
from .process_manager import ProcessManager
from .scheduler import SchedulerThread
from .state import SupervisorState


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Clonoth Supervisor")
    parser.add_argument("--host", default=os.getenv("CLONOTH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CLONOTH_PORT", "8765")))
    parser.add_argument("--no-shell", action="store_true", help="do not spawn shell runtime")
    parser.add_argument("--no-kernel", action="store_true", help="do not spawn kernel runtime")
    parser.add_argument("--no-workers", action="store_true", help="do not spawn any workers")
    parser.add_argument("--log-level", default=os.getenv("CLONOTH_LOG_LEVEL", "info"))
    parser.add_argument(
        "--access-log",
        action="store_true",
        help="enable uvicorn access log (VERY noisy because workers poll endpoints frequently)",
    )
    args = parser.parse_args()

    workspace_root = Path(__file__).resolve().parents[1]
    data_dir = workspace_root / "data"
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # ---- 把 supervisor 自身的所有输出重定向到日志文件 ----
    # 这样 TUI 可以在同一终端前台运行，不会被 uvicorn 日志覆盖。
    _supervisor_log_path = log_dir / "supervisor.log"
    _supervisor_log_f = open(_supervisor_log_path, "a", encoding="utf-8", buffering=1)

    def _log(msg: str) -> None:
        """写 supervisor 日志（不写终端）。"""
        try:
            _supervisor_log_f.write(msg + "\n")
            _supervisor_log_f.flush()
        except Exception:
            pass

    events_path = data_dir / "events.jsonl"
    config_path = data_dir / "config.yaml"

    run_id = os.urandom(16).hex()

    eventlog = EventLog(events_path, run_id=run_id)
    policy = PolicyEngine(workspace_root=workspace_root)
    state = SupervisorState(workspace_root=workspace_root, eventlog=eventlog, policy=policy)

    config_store = ConfigStore(path=config_path)
    state.write_boot_event()

    # 在拉起子进程之前，先检查端口是否可用
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            _s.bind((args.host, args.port))
    except OSError as e:
        _log(f"[supervisor] 端口 {args.host}:{args.port} 已被占用: {e}")
        return

    base_url = f"http://{args.host}:{args.port}"

    process_manager: ProcessManager | None = None
    if not args.no_workers:
        process_manager = ProcessManager(
            supervisor_url=base_url,
            workspace_root=workspace_root,
            log_dir=data_dir / "logs",
            log_func=_log,
        )
        if not args.no_kernel:
            process_manager.start_engine()
        if not args.no_shell and process_manager.spawn_shell_cli:
            process_manager.start_shell_cli()

    scheduler = SchedulerThread(state=state, workspace_root=workspace_root)
    scheduler.start()

    app = create_app(state=state, process_manager=process_manager, config_store=config_store)

    env_access_log = (os.getenv("CLONOTH_ACCESS_LOG") or "").strip().lower() in {"1", "true", "yes", "y"}
    access_log = bool(args.access_log or env_access_log)

    # TUI 子进程退出时自动关停 supervisor
    if process_manager and process_manager.shell_cli:
        _shell_proc = process_manager.shell_cli
        _pm = process_manager

        def _watch_shell() -> None:
            """后台线程：等待 TUI 进程退出，根据 restart 标记决定重启或退出。"""
            try:
                _shell_proc.popen.wait()
            except Exception:
                pass

            restart = _pm._restart_pending

            if restart:
                _log("[supervisor] TUI 已退出，正在重启...")
            else:
                _log("[supervisor] TUI 已退出，正在关闭...")

            try:
                _pm.stop_engine()
            except Exception:
                pass

            # 退出码 75 = 请求重启（由 main.py 外层循环检测）
            _code = 75 if restart else 0
            _log(f"[supervisor] exit code={_code}")
            os._exit(_code)

        threading.Thread(target=_watch_shell, daemon=True, name="shell-watcher").start()

    # uvicorn 日志全部写到文件，不输出到终端（终端留给 TUI）
    _uvi_log_cfg = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "file": {
                "class": "logging.FileHandler",
                "filename": str(_supervisor_log_path),
                "mode": "a",
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["file"], "level": "INFO", "propagate": False},
        },
    }

    _log(f"[supervisor] starting uvicorn on {args.host}:{args.port}")
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=access_log,
        log_config=_uvi_log_cfg,
    )


if __name__ == "__main__":
    main()
