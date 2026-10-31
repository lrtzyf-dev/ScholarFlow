"""桌面通知（Windows 原生 Toast）。

零第三方依赖：通过系统自带的 Windows PowerShell 5.1 调用 WinRT
`Windows.UI.Notifications` 弹出原生 toast，用于后台任务完成 / 失败 / 需审批时提醒。

设计要点：
- 标题与正文经环境变量传入（避免中文与特殊字符的引号/编码问题）；
- 固定脚本用 `-EncodedCommand`（UTF-16LE Base64）下发，彻底规避命令行转义；
- 任何失败都静默降级，绝不影响 Agent 主流程。
"""
from __future__ import annotations

import base64
import os
import platform
import subprocess

# 任意 AppID 即可在 Win10/11 弹出 toast（图标用系统默认）
APP_ID = "ScholarFlow.ScholarFlow"

# 固定 PowerShell 脚本：从环境变量读取标题/正文，做 XML 转义后弹 toast
_PS_SCRIPT = r"""
$ErrorActionPreference = "SilentlyContinue"
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

  function Esc([string]$s) {
    if ($null -eq $s) { return "" }
    return $s.Replace("&","&amp;").Replace("<","&lt;").Replace(">","&gt;").Replace('"',"&quot;").Replace("'","&apos;")
  }

  $title = Esc $env:SF_NOTIFY_TITLE
  $msg   = Esc $env:SF_NOTIFY_MSG
  $appId = $env:SF_NOTIFY_APPID
  if (-not $appId) { $appId = "ScholarFlow.ScholarFlow" }

  $xmlString = @"
<toast><visual><binding template="ToastGeneric"><text>$title</text><text>$msg</text></binding></visual></toast>
"@
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $xml.LoadXml($xmlString)
  $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
} catch { }
"""


def _encoded_command(script: str) -> str:
    """把 PowerShell 脚本编码为 -EncodedCommand 所需的 UTF-16LE Base64。"""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def notify(title: str, message: str = "", level: str = "info") -> dict:
    """弹出桌面通知。

    返回 {sent, reason?}：
    - 关闭通知 / 非 Windows 平台 / 调用异常时 sent=False，且不抛异常。
    """
    from .config import settings

    if not getattr(settings, "desktop_notifications", True):
        return {"sent": False, "reason": "disabled"}
    if platform.system() != "Windows":
        return {"sent": False, "reason": "unsupported"}

    env = dict(os.environ)
    env["SF_NOTIFY_TITLE"] = (title or "ScholarFlow")[:200]
    env["SF_NOTIFY_MSG"] = (message or "")[:500]
    env["SF_NOTIFY_APPID"] = APP_ID

    try:
        proc = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden", "-EncodedCommand",
                _encoded_command(_PS_SCRIPT),
            ],
            env=env, timeout=8, capture_output=True,
        )
        return {"sent": proc.returncode == 0, "returncode": proc.returncode}
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "reason": f"{type(exc).__name__}: {exc}"}
