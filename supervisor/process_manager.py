from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from clonoth_runtime import get_bool, get_float, get_int, get_str, load_runtime_config


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _make_child_preexec():
    """返回 preexec_fn：Linux 上设置 PR_SET_PDEATHSIG，父进程退出时子进程自动收到 SIGTERM。"""
    if sys.platform == "linux":
        def _set_pdeathsig():
            try:
                import ctypes
                import ctypes.util
                libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
                PR_SET_PDEATHSIG = 1
                libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
            except Exception:
                pass
        return _set_pdeathsig
    return None


@dataclass
class ManagedProcess:
    name: str
    popen: subprocess.Popen
    log_path: Path | None


class ProcessManager:
    """管理引擎 worker 进程。"""

    def __init__(self, *, supervisor_url: str, workspace_root: Path, log_dir: Path, log_func=None) -> None:
        self.supervisor_url = supervisor_url
        self.workspace_root = workspace_root
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.engines: list[ManagedProcess] = []
        self.discord_adapter: ManagedProcess | None = None
        self.onebot_adapter: ManagedProcess | None = None
        self.napcat: ManagedProcess | None = None
        self._stopped = False
        self._restart_pending = False
        self._restarting_engine = False  # restart engine 期间抑制信号退出
        # Set by supervisor.main to ask the HTTP server to exit gracefully. Raising
        # SystemExit from the signal handler would abort the event loop before
        # anyio's worker threads are told to stop, and the interpreter then waits
        # on those non-daemon threads until systemd SIGKILLs the process.
        self.request_shutdown: Callable[[], None] | None = None

        # 日志函数：写日志文件而不是终端。
        self._log = log_func or (lambda msg: print(msg))

        runtime_cfg = load_runtime_config(workspace_root)

        self.stop_wait_timeout_sec = get_float(
            runtime_cfg,
            "supervisor.process_manager.stop_wait_timeout_sec",
            5.0,
            min_value=0.1,
            max_value=30.0,
        )
        self.engine_workers = get_int(
            runtime_cfg,
            "supervisor.process_manager.engine_workers",
            2,
            min_value=1,
            max_value=8,
        )

        self.spawn_discord_adapter = get_bool(runtime_cfg, "supervisor.process_manager.spawn_discord_adapter", False)
        self.discord_entry_node = get_str(runtime_cfg, "supervisor.process_manager.discord_entry_node", "ereuna_main")
        self.discord_bridge_port = get_int(runtime_cfg, "supervisor.process_manager.discord_bridge_port", 8768, min_value=1, max_value=65535)
        self.discord_history_len = get_int(runtime_cfg, "supervisor.process_manager.discord_history_len", 15, min_value=1, max_value=200)
        self.discord_log_channel = get_str(runtime_cfg, "supervisor.process_manager.discord_log_channel", "")
        self.discord_superusers = get_str(runtime_cfg, "supervisor.process_manager.discord_superusers", "")
        self.discord_proxy = get_str(runtime_cfg, "supervisor.process_manager.discord_proxy", "")
        self.discord_retry_delay = get_int(runtime_cfg, "supervisor.process_manager.discord_retry_delay", 10, min_value=1, max_value=300)
        self.discord_voice_input = get_bool(runtime_cfg, "supervisor.process_manager.discord_voice_input", True)
        self.discord_voice_reply = get_bool(runtime_cfg, "supervisor.process_manager.discord_voice_reply", True)
        self.discord_voice_reply_on_voice_input = get_bool(runtime_cfg, "supervisor.process_manager.discord_voice_reply_on_voice_input", True)
        self.discord_tts_format = get_str(runtime_cfg, "supervisor.process_manager.discord_tts_format", "wav")
        self.discord_tts_voice = get_str(runtime_cfg, "supervisor.process_manager.discord_tts_voice", "")
        self.discord_tts_instruction = get_str(runtime_cfg, "supervisor.process_manager.discord_tts_instruction", "")
        self.discord_delete_audio_after_use = get_bool(runtime_cfg, "supervisor.process_manager.discord_delete_audio_after_use", True)
        self.spawn_onebot_adapter = get_bool(runtime_cfg, "supervisor.process_manager.spawn_onebot_adapter", False)
        self.spawn_napcat = get_bool(runtime_cfg, "supervisor.process_manager.spawn_napcat", False)
        self.onebot_host = get_str(runtime_cfg, "supervisor.process_manager.onebot_host", "127.0.0.1")
        self.onebot_port = get_int(runtime_cfg, "supervisor.process_manager.onebot_port", 18080, min_value=1, max_value=65535)
        self.onebot_entry_node = get_str(runtime_cfg, "supervisor.process_manager.onebot_entry_node", "qq.orchestrator")
        self.napcat_quick_login = get_str(runtime_cfg, "supervisor.process_manager.napcat_quick_login", "")
        self.onebot_owner_id = get_str(runtime_cfg, "supervisor.process_manager.onebot_owner_id", "")
        self.onebot_owner_name = get_str(runtime_cfg, "supervisor.process_manager.onebot_owner_name", "")
        self.onebot_voice_input = get_bool(runtime_cfg, "supervisor.process_manager.onebot_voice_input", True)
        self.onebot_voice_reply = get_bool(runtime_cfg, "supervisor.process_manager.onebot_voice_reply", False)
        self.onebot_voice_reply_on_voice_input = get_bool(runtime_cfg, "supervisor.process_manager.onebot_voice_reply_on_voice_input", True)
        self.onebot_voice_reply_auto_on_text = get_bool(runtime_cfg, "supervisor.process_manager.onebot_voice_reply_auto_on_text", True)
        self.onebot_voice_reply_private = get_bool(runtime_cfg, "supervisor.process_manager.onebot_voice_reply_private", True)
        self.onebot_voice_reply_group = get_bool(runtime_cfg, "supervisor.process_manager.onebot_voice_reply_group", True)
        self.onebot_delete_audio_after_use = get_bool(runtime_cfg, "supervisor.process_manager.onebot_delete_audio_after_use", True)
        self.onebot_tts_format = get_str(runtime_cfg, "supervisor.process_manager.onebot_tts_format", "wav")
        self.onebot_tts_voice = get_str(runtime_cfg, "supervisor.process_manager.onebot_tts_voice", "")
        self.onebot_tts_instruction = get_str(runtime_cfg, "supervisor.process_manager.onebot_tts_instruction", "")

        # 注册清理：supervisor 退出时杀掉所有子进程
        atexit.register(self._cleanup)
        self._install_signal_handlers()

    def _install_signal_handlers(self) -> None:
        """拦截 SIGTERM/SIGINT，先清理子进程再退出。"""
        for sig in (signal.SIGTERM, signal.SIGINT):
            prev_handler = signal.getsignal(sig)

            def _handler(signum, frame, _prev=prev_handler):
                import traceback as _tb
                try:
                    _sname = signal.Signals(signum).name
                except (ValueError, AttributeError):
                    _sname = str(signum)
                _tname = threading.current_thread().name
                # restart engine 期间信号泄漏到 supervisor，忽略不退出
                if self._restarting_engine:
                    return
                self._cleanup()
                if self.request_shutdown is not None:
                    self.request_shutdown()
                    return
                # 调用原有的 handler
                if callable(_prev) and _prev not in (signal.SIG_IGN, signal.SIG_DFL):
                    _prev(signum, frame)
                else:
                    sys.exit(0)

            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                # 非主线程中无法设置信号处理
                pass

    def _cleanup(self) -> None:
        """清理所有子进程。可重入安全。"""
        if self._stopped:
            return
        self._stopped = True
        self._log("[process_manager] cleaning up child processes...")
        self.stop_all()

    def _spawn(
        self,
        *,
        name: str,
        module: str | None = None,
        script_path: Path | None = None,
        extra_args: list[str] | None = None,
        capture_output: bool = True,
        new_console: bool = False,
        extra_env: dict[str, str] | None = None,
        pass_supervisor_arg: bool = True,
    ) -> ManagedProcess:
        log_path = self.log_dir / f"{name}-{_timestamp()}.log" if capture_output else None
        stdout = None
        stderr = None
        if capture_output and log_path:
            log_f = log_path.open("a", encoding="utf-8")
            stdout = log_f
            stderr = subprocess.STDOUT

        env = {**os.environ, "CLONOTH_SUPERVISOR_URL": self.supervisor_url}
        if extra_env:
            env.update(extra_env)

        if script_path is not None:
            cmd = [sys.executable, str(script_path)]
        elif module:
            cmd = [sys.executable, "-m", module]
        else:
            raise ValueError("module or script_path is required")
        if pass_supervisor_arg:
            cmd.extend(["--supervisor", self.supervisor_url])
        if extra_args:
            cmd.extend(extra_args)

        creationflags = 0
        preexec_fn = None
        if sys.platform == "win32" and new_console:
            creationflags = subprocess.CREATE_NEW_CONSOLE
        elif sys.platform != "win32":
            # Linux/macOS: 父进程退出时子进程自动收到 SIGTERM
            preexec_fn = _make_child_preexec()

        p = subprocess.Popen(
            cmd,
            cwd=str(self.workspace_root),
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
            start_new_session=True,  # engine 独立进程组，杀 engine 不波及 supervisor
        )
        self._log(f"[process_manager] spawned {name} pid={p.pid}")
        return ManagedProcess(name=name, popen=p, log_path=log_path)

    def start_engine(self) -> None:
        self._stopped = False
        self.engines = [proc for proc in self.engines if proc.popen.poll() is None]
        while len(self.engines) < self.engine_workers:
            idx = len(self.engines) + 1
            name = f"engine-{idx}"
            proc = self._spawn(
                name=name,
                module="engine",
                capture_output=True,
                new_console=False,
                extra_args=["--worker-id", name],
            )
            self.engines.append(proc)

    def start_discord_adapter(self) -> None:
        if self.discord_adapter and self.discord_adapter.popen.poll() is None:
            return
        extra_env = {
            "CLONOTH_URL": self.supervisor_url,
            "CLONOTH_BASE_URL": self.supervisor_url,
            "CLONOTH_WORKSPACE": str(self.workspace_root),
            "DISCORD_ENTRY_NODE": self.discord_entry_node,
            "DISCORD_BRIDGE_PORT": str(self.discord_bridge_port),
            "DISCORD_HISTORY_LEN": str(self.discord_history_len),
            "DISCORD_RETRY_DELAY": str(self.discord_retry_delay),
            "CLONOTH_DISCORD_VOICE_INPUT": str(self.discord_voice_input).lower(),
            "CLONOTH_DISCORD_VOICE_REPLY": str(self.discord_voice_reply).lower(),
            "CLONOTH_DISCORD_VOICE_REPLY_ON_VOICE_INPUT": str(self.discord_voice_reply_on_voice_input).lower(),
            "CLONOTH_DISCORD_TTS_FORMAT": self.discord_tts_format,
            "CLONOTH_DISCORD_TTS_VOICE": self.discord_tts_voice,
            "CLONOTH_DISCORD_TTS_INSTRUCTION": self.discord_tts_instruction,
            "CLONOTH_DISCORD_DELETE_AUDIO_AFTER_USE": str(self.discord_delete_audio_after_use).lower(),
        }
        if self.discord_log_channel:
            extra_env["DISCORD_LOG_CHANNEL"] = self.discord_log_channel
        if self.discord_superusers:
            extra_env["DISCORD_SUPERUSERS"] = self.discord_superusers
        if self.discord_proxy:
            extra_env["DISCORD_PROXY"] = self.discord_proxy
        self.discord_adapter = self._spawn(
            name="discord-adapter",
            module="adapters.discord.app",
            capture_output=True,
            new_console=False,
            pass_supervisor_arg=False,
            extra_env=extra_env,
        )

    def start_onebot_adapter(self) -> None:
        if self.onebot_adapter and self.onebot_adapter.popen.poll() is None:
            return
        self.onebot_adapter = self._spawn(
            name="onebot-adapter",
            # Run app.py as a script: `-m adapters.onebot.app` imports the
            # adapters.onebot package first, whose import calls get_driver()
            # before nonebot.init() and crashes.
            script_path=self.workspace_root / "adapters" / "onebot" / "app.py",
            capture_output=True,
            new_console=False,
            pass_supervisor_arg=False,
            extra_env={
                "ONEBOT_HOST": self.onebot_host,
                "ONEBOT_PORT": str(self.onebot_port),
                "CLONOTH_BASE_URL": self.supervisor_url,
                "CLONOTH_URL": self.supervisor_url,
                "CLONOTH_WORKSPACE": str(self.workspace_root),
                "CLONOTH_ENTRY_NODE": self.onebot_entry_node,
                "CLONOTH_QQ_OWNER_ID": self.onebot_owner_id,
                "CLONOTH_QQ_OWNER_NAME": self.onebot_owner_name,
                "CLONOTH_ONEBOT_VOICE_INPUT": str(self.onebot_voice_input).lower(),
                "CLONOTH_ONEBOT_VOICE_REPLY": str(self.onebot_voice_reply).lower(),
                "CLONOTH_ONEBOT_VOICE_REPLY_ON_VOICE_INPUT": str(self.onebot_voice_reply_on_voice_input).lower(),
                "CLONOTH_ONEBOT_VOICE_REPLY_AUTO_ON_TEXT": str(self.onebot_voice_reply_auto_on_text).lower(),
                "CLONOTH_ONEBOT_VOICE_REPLY_PRIVATE": str(self.onebot_voice_reply_private).lower(),
                "CLONOTH_ONEBOT_VOICE_REPLY_GROUP": str(self.onebot_voice_reply_group).lower(),
                "CLONOTH_ONEBOT_DELETE_AUDIO_AFTER_USE": str(self.onebot_delete_audio_after_use).lower(),
                "CLONOTH_ONEBOT_TTS_FORMAT": self.onebot_tts_format,
                "CLONOTH_ONEBOT_TTS_VOICE": self.onebot_tts_voice,
                "CLONOTH_ONEBOT_TTS_INSTRUCTION": self.onebot_tts_instruction,
            },
        )

    def start_napcat(self) -> None:
        if self.napcat and self.napcat.popen.poll() is None:
            return
        self.napcat = self._spawn(
            name="napcat",
            module="adapters.onebot.napcat_manager",
            extra_args=[
                "start",
                "--reverse-host", self.onebot_host,
                "--reverse-port", str(self.onebot_port),
                "--reverse-suffix", "/onebot/v11/ws",
                "--quick-login", self.napcat_quick_login,
            ],
            capture_output=True,
            new_console=False,
            pass_supervisor_arg=False,
        )

    def stop_engine(self) -> None:
        for proc in list(self.engines):
            self._stop(proc)
        self.engines = []

    def stop_discord_adapter(self) -> None:
        if self.discord_adapter:
            self._stop(self.discord_adapter)
            self.discord_adapter = None

    def stop_onebot_adapter(self) -> None:
        if self.onebot_adapter:
            self._stop(self.onebot_adapter)
            self.onebot_adapter = None

    def stop_napcat(self) -> None:
        if self.napcat is None:
            return
        self._stop(self.napcat)
        self.napcat = None
        try:
            result = subprocess.run(
                [sys.executable, str(self.workspace_root / "adapters" / "onebot" / "napcat_manager.py"), "stop"],
                cwd=str(self.workspace_root),
                timeout=10,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode:
                self._log(f"[process_manager] napcat stop failed: exit {result.returncode}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._log(f"[process_manager] napcat stop failed: {exc}")

    def restart_engine(self) -> None:
        self.stop_engine()
        self.start_engine()

    def stop_all(self) -> None:
        self.stop_discord_adapter()
        self.stop_onebot_adapter()
        self.stop_napcat()
        self.stop_engine()

    def _stop(self, proc: ManagedProcess) -> None:
        p = proc.popen
        if p.poll() is not None:
            return
        try:
            p.terminate()
            p.wait(timeout=float(self.stop_wait_timeout_sec))
        except Exception:
            try:
                p.kill()
                p.wait(timeout=5)  # reap after SIGKILL to release ports
            except Exception:
                pass
        self._log(f"[process_manager] stopped {proc.name} pid={p.pid}")
