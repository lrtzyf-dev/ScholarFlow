"""研序 ScholarFlow 桌面启动器。

主进程：后台线程运行 uvicorn（FastAPI + 内嵌前端），主线程运行 pywebview 窗口。
原生交互式终端由独立辅助进程承载。
窗口关闭后服务自动退出。

开发环境运行：
    cd backend && python ..\\desktop\\launcher.py
打包后：由 PyInstaller 生成 ScholarFlow.exe。
"""
from __future__ import annotations

import os
import socket
import sys

# This private mode shares the packaged runtime, not the desktop service or UI.
# Dispatch before importing webview/uvicorn and claiming a data-directory lock.
if __name__ == '__main__' and sys.argv[1:] == ['--terminal-worker']:
    from app.terminal_worker import main as terminal_worker_main
    terminal_worker_main()
    raise SystemExit(0)

import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
import webview

APP_TITLE = "研序 ScholarFlow"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _icon_path() -> str | None:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent
    icon = base / "assets" / "icon.ico"
    return str(icon) if icon.exists() else None


def _wait_ready(port: int, server, timeout: float = 30.0, *, thread=None) -> bool:
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if server.should_exit or (thread is not None and not thread.is_alive()):
            return False
        if not server.started:
            time.sleep(0.1)
            continue
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except OSError:
            time.sleep(0.2)
    return False


def _write_crash(exc_type, exc, tb) -> None:
    """窗口模式下没有控制台，把崩溃信息写入数据目录。"""
    import traceback

    try:
        from app.config import DATA_DIR

        log = DATA_DIR / "startup_error.log"
    except Exception:
        log = Path.home() / "ScholarFlow_startup_error.log"
    try:
        with open(log, "a", encoding="utf-8") as f:
            f.write("".join(traceback.format_exception(exc_type, exc, tb)))
            f.write("\n")
    except OSError:
        pass


def _install_crash_log() -> None:
    sys.excepthook = _write_crash

    def _thread_hook(args):
        _write_crash(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = _thread_hook


class WindowActivation:
    def __init__(self, window):
        self.window = window
        self.minimized = False
        self.maximized = False
        self._guard = threading.Lock()
        window.events.minimized += self._minimized
        window.events.maximized += self._maximized
        window.events.restored += self._restored

    def _minimized(self):
        with self._guard:
            self.minimized = True

    def _maximized(self):
        with self._guard:
            self.minimized, self.maximized = False, True

    def _restored(self):
        with self._guard:
            self.minimized = self.maximized = False

    def __call__(self):
        if not self.window.events.shown.is_set():
            return False
        with self._guard:
            minimized, maximized = self.minimized, self.maximized
        if minimized:
            self.window.restore()
            if maximized:
                self.window.maximize()
        self.window.show()
        return True


def _show_busy(service=False):
    message = ("此数据目录已有研序后台服务正在运行，请先关闭该服务。" if service else
               "研序正在启动或关闭，暂时无法显示窗口，请稍后重试。")
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x40)
    else:
        print(message, file=sys.stderr)


class DesktopApi:
    def __init__(self):
        self._window = None
        self._picker_lock = threading.Lock()

    def choose_project_folder(self):
        if self._window is None or not self._picker_lock.acquire(blocking=False):
            return None
        try:
            selected = self._window.create_file_dialog(webview.FileDialog.FOLDER, allow_multiple=False)
            return str(selected[0]) if selected else None
        finally:
            self._picker_lock.release()


def main() -> int:
    _install_crash_log()
    # 开发模式下把 backend 加入模块搜索路径
    if not getattr(sys, "frozen", False):
        backend_dir = Path(__file__).resolve().parent.parent / "backend"
        if backend_dir.exists():
            sys.path.insert(0, str(backend_dir))

    from app.runtime_owner import owner, claim_or_activate

    admission = claim_or_activate(owner)
    if admission == "activated":
        return 0
    if admission != "primary":
        _show_busy(admission == "service_busy")
        return 2

    server = server_thread = None
    try:
        # Reserve before importing the app, migrating data or recovering tasks.
        from app.main import app as fastapi_app

        port = _free_port()
        owner.set_desktop_port(port)
        server = uvicorn.Server(uvicorn.Config(
            fastapi_app, host="127.0.0.1", port=port, log_level="warning",
            access_log=False, timeout_graceful_shutdown=5,
        ))
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        if not _wait_ready(port, server, thread=server_thread):
            print("研序服务启动失败", file=sys.stderr)
            return 1

        desktop_api = DesktopApi()
        window = webview.create_window(
            APP_TITLE, f"http://127.0.0.1:{port}", width=1440, height=900,
            min_size=(1100, 700), text_select=True, js_api=desktop_api,
        )
        desktop_api._window = window
        owner.set_activation(WindowActivation(window))
        window.events.closing += owner.stopping
        webview.start(icon=_icon_path())
        return 0
    finally:
        owner.stopping()
        run_store = sys.modules.get("app.run_store")
        task_runner = sys.modules.get("app.task_runner")
        try:
            if run_store is not None:
                run_store.cancel_all()
        finally:
            try:
                if task_runner is not None:
                    task_runner.request_shutdown()
            finally:
                if server is not None:
                    server.should_exit = True
                if server_thread is not None:
                    server_thread.join(timeout=7)
                if server_thread is None or not server_thread.is_alive():
                    active = ((task_runner is not None and task_runner.running_task_ids()) or
                              (run_store is not None and run_store.active_runs()))
                    owner.finish_shutdown(idle=not active)


if __name__ == "__main__":
    raise SystemExit(main())
