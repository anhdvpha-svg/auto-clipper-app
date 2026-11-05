"""Settings and paths. API keys are encrypted with Windows DPAPI (tied to the Windows user account)."""
import base64, json, os, sys

APP_NAME = "AutoClipper"


def data_dir():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def default_output_dir():
    d = os.path.join(os.path.expanduser("~"), "Videos", APP_NAME)
    return d


def resource_path(*parts):
    """Files shipped with the app (fonts, icon). Works both frozen (PyInstaller) and from source."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


# ---------- secret storage ----------

def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if protect:
        ok = fn(ctypes.byref(inp), "AutoClipper", None, None, None, 0, ctypes.byref(out))
    else:
        ok = fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(secret: str) -> str:
    if not secret:
        return ""
    raw = secret.encode("utf-8")
    if os.name == "nt":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode()
        except Exception:
            pass
    return "plain:" + base64.b64encode(raw).decode()


def unprotect(stored: str) -> str:
    if not stored:
        return ""
    kind, _, payload = stored.partition(":")
    try:
        raw = base64.b64decode(payload)
        if kind == "dpapi":
            raw = _dpapi(raw, False)
        return raw.decode("utf-8")
    except Exception:
        return ""


# ---------- config file ----------

DEFAULTS = {
    "provider": "offline",            # offline | claude | openai | gemini
    "models": {"claude": "claude-opus-5-5", "openai": "", "gemini": ""},
    "keys": {"claude": "", "openai": "", "gemini": ""},   # stored protected
    "num_clips": 0,                   # 0 = automatic
    "length": "30-60",
    "topic": "",
    "language": "auto",
    "captions": True,
    "uppercase": True,
    "highlight": "FFE600",
    "framing": "auto",                # auto | center | blur
    "hook": True,
    "whisper_model": "small",
    "output_dir": "",
    "browser": "",                    # "" | firefox | chrome | edge | brave - YouTube login fallback
}


def _path():
    return os.path.join(data_dir(), "config.json")


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(_path(), encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    if not cfg.get("output_dir"):
        cfg["output_dir"] = default_output_dir()
    return cfg


def save(cfg):
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())


def get_key(cfg, provider):
    return unprotect(cfg.get("keys", {}).get(provider, ""))


def set_key(cfg, provider, key):
    cfg.setdefault("keys", {})[provider] = protect(key.strip())
