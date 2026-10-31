"""桌面通知模块单元测试（mock 子进程，不真正弹 toast）。"""
import subprocess

from app import notifier
from app.config import settings


def test_notify_disabled(monkeypatch):
    """关闭通知开关：不调用子进程，返回 disabled。"""
    monkeypatch.setattr(settings, "desktop_notifications", False)
    called = []
    monkeypatch.setattr(notifier.subprocess, "run",
                        lambda *a, **k: called.append(a) or _ok_proc())
    result = notifier.notify("t", "m")
    assert result["sent"] is False
    assert result["reason"] == "disabled"
    assert not called


def test_notify_non_windows(monkeypatch):
    """非 Windows 平台：返回 unsupported。"""
    monkeypatch.setattr(settings, "desktop_notifications", True)
    monkeypatch.setattr(notifier.platform, "system", lambda: "Linux")
    result = notifier.notify("t", "m")
    assert result["sent"] is False
    assert result["reason"] == "unsupported"


def test_notify_windows_calls_powershell(monkeypatch):
    """Windows 正常路径：以 EncodedCommand 调 powershell，返回 sent=True。"""
    monkeypatch.setattr(settings, "desktop_notifications", True)
    monkeypatch.setattr(notifier.platform, "system", lambda: "Windows")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _ok_proc()

    monkeypatch.setattr(notifier.subprocess, "run", fake_run)
    result = notifier.notify("标题", "正文")
    assert result["sent"] is True
    assert captured["args"][0] == "powershell.exe"
    assert "-EncodedCommand" in captured["args"]
    # 标题经环境变量传入
    assert captured["kwargs"]["env"]["SF_NOTIFY_TITLE"] == "标题"


def test_notify_swallows_exception(monkeypatch):
    """子进程异常：静默降级，不抛错。"""
    monkeypatch.setattr(settings, "desktop_notifications", True)
    monkeypatch.setattr(notifier.platform, "system", lambda: "Windows")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=8)

    monkeypatch.setattr(notifier.subprocess, "run", boom)
    result = notifier.notify("t", "m")
    assert result["sent"] is False
    assert "TimeoutExpired" in result["reason"]


def _ok_proc():
    class _P:
        returncode = 0

    return _P()
