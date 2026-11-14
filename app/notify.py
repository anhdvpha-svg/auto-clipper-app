"""'Done' notifications: a Windows notification (Action Center) + taskbar flash + sound when the app is not in
front; the in-app toast always. No extra packages: the notification goes through Windows' own toast API."""
import os, subprocess, threading

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# Windows only shows toasts for a registered app id; PowerShell's is always there
_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{body}</text></binding></visual></toast>')
$t = New-Object Windows.UI.Notifications.ToastNotification $x
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{aumid}').Show($t)
"""


def _xml(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&apos;")
            .replace('"', "&quot;"))[:200]


def system_toast(title, body):
    if os.name != "nt":
        return
    script = _PS.replace("{title}", _xml("Auto Clipper · " + title)).replace("{body}", _xml(body)) \
        .replace("{aumid}", _AUMID)

    def run():
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                            script], timeout=20, capture_output=True, stdin=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def flash_and_beep(tk_root, ok=True):
    if os.name != "nt":
        try:
            tk_root.bell()
        except Exception:
            pass
        return
    try:
        import ctypes, winsound
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
        hwnd = ctypes.windll.user32.GetParent(tk_root.winfo_id()) or tk_root.winfo_id()
        fi = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)  # flash caption+tray until focused
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(fi))
        winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
    except Exception:
        pass


def app_has_focus(tk_root):
    try:
        return tk_root.focus_displayof() is not None and tk_root.state() != "iconic"
    except Exception:
        return False
