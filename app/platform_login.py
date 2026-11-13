"""Log in to TikTok, Instagram, Facebook, X... once inside the app, and reuse that session for every download.

The login window is a small embedded browser (Edge WebView2 through pywebview) running in its own process. Its
profile is kept on disk, so the site remembers the user; as soon as the site's session cookie appears the cookies
are written in Netscape format (cookies.txt) and yt-dlp is given that file for links from that site."""
import calendar, email.utils, json, os, subprocess, sys, threading, time
from urllib.parse import urlparse

from . import config

SITES = {
    "tiktok": {"label": "TikTok", "login": "https://www.tiktok.com/login", "auth": ["sessionid", "sid_tt"],
               "domains": ["tiktok.com"]},
    "instagram": {"label": "Instagram", "login": "https://www.instagram.com/accounts/login/", "auth": ["sessionid"],
                  "domains": ["instagram.com"]},
    "facebook": {"label": "Facebook", "login": "https://www.facebook.com/login", "auth": ["c_user"],
                 "domains": ["facebook.com", "fb.watch"]},
    "x": {"label": "X (Twitter)", "login": "https://x.com/i/flow/login", "auth": ["auth_token"],
          "domains": ["x.com", "twitter.com"]},
    "twitch": {"label": "Twitch", "login": "https://www.twitch.tv/login", "auth": ["auth-token"],
               "domains": ["twitch.tv"]},
    "youtube": {"label": "YouTube", "login": "https://accounts.google.com/ServiceLogin?service=youtube&continue="
                                            "https%3A%2F%2Fwww.youtube.com%2F", "auth": ["LOGIN_INFO", "SAPISID"],
                "domains": ["youtube.com", "youtu.be"],
                "note": "Google đôi khi chặn đăng nhập trong cửa sổ nhúng; khi đó dùng trình duyệt Firefox trong Cài đặt."},
}
# where the session lives once logged in (cookies are read from this page)
HOME = {"tiktok": "https://www.tiktok.com/", "instagram": "https://www.instagram.com/",
        "facebook": "https://www.facebook.com/", "x": "https://x.com/home", "twitch": "https://www.twitch.tv/",
        "youtube": "https://www.youtube.com/"}


def cookies_dir():
    d = os.path.join(config.data_dir(), "cookies")
    os.makedirs(d, exist_ok=True)
    return d


def cookie_path(site):
    return os.path.join(cookies_dir(), f"{site}.txt")


def site_of_url(url):
    host = (urlparse(url or "").hostname or "").lower()
    for key, s in SITES.items():
        if any(host == d or host.endswith("." + d) for d in s["domains"]):
            return key
    return None


def is_logged_in(site):
    p = cookie_path(site)
    if not os.path.exists(p):
        return False
    try:
        meta = json.load(open(p + ".json", encoding="utf-8"))
        return meta.get("logged_in", True)
    except Exception:
        return True


def cookies_for_url(url):
    """cookies.txt saved by the in-app login for this link's site, or None."""
    site = site_of_url(url)
    return cookie_path(site) if site and is_logged_in(site) else None


def logout(site):
    for p in (cookie_path(site), cookie_path(site) + ".json"):
        try:
            os.remove(p)
        except OSError:
            pass


def start_login(site):
    """Open the login window in its own process (the browser engine wants its own main thread)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--login", site]
    else:
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_app.py"),
               "--login", site]
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL)


# ---------------- cookies.txt ----------------

def _expiry(value):
    try:
        dt = email.utils.parsedate_to_datetime(value)
        ts = calendar.timegm(dt.utctimetuple())
        return ts if ts > time.time() else 0
    except Exception:
        return 0


def netscape_lines(cookies):
    """pywebview cookies (SimpleCookie objects) -> Netscape cookies.txt lines (what yt-dlp reads)."""
    out = ["# Netscape HTTP Cookie File", "# Written by Auto Clipper (in-app login)", ""]
    seen = set()
    for ck in cookies:
        for name, m in ck.items():
            domain = m["domain"] or ""
            if not domain:
                continue
            key = (domain, m["path"] or "/", name)
            if key in seen:
                continue
            seen.add(key)
            if not domain.startswith(".") and domain.count(".") == 1:
                domain = "." + domain  # "tiktok.com" from WebView2 = a cookie for the whole site
            sub = "TRUE" if domain.startswith(".") else "FALSE"
            prefix = "#HttpOnly_" if m["httponly"] else ""
            secure = "TRUE" if m["secure"] else "FALSE"
            out.append("\t".join([prefix + domain, sub, m["path"] or "/", secure, str(_expiry(m["expires"])),
                                  name, m.value]))
    return out


def save_cookies(site, cookies):
    names = {n for ck in cookies for n in ck.keys()}
    ok = any(a in names for a in SITES[site]["auth"])
    lines = netscape_lines(cookies)
    tmp = cookie_path(site) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, cookie_path(site))
    json.dump({"logged_in": ok, "saved": time.time(), "count": len(lines) - 3},
              open(cookie_path(site) + ".json", "w", encoding="utf-8"))
    return ok


# ---------------- the login window (runs in its own process) ----------------

BANNER_JS = """(function(){var d=document.getElementById('ac-banner');if(!d){d=document.createElement('div');
d.id='ac-banner';d.style.cssText='position:fixed;z-index:2147483647;left:50%%;top:12px;transform:translateX(-50%%);'+
'background:#22D3EE;color:#03161A;font:600 15px Segoe UI,Arial;padding:10px 18px;border-radius:10px;'+
'box-shadow:0 6px 24px rgba(0,0,0,.35)';document.body.appendChild(d);}d.textContent=%s;})();"""


def run_login_window(site, close_after=None):
    import webview
    s = SITES[site]
    storage = os.path.join(config.data_dir(), "webview")
    os.makedirs(storage, exist_ok=True)
    win = webview.create_window(f"Đăng nhập {s['label']} · Auto Clipper", s["login"], width=1060, height=820)
    state = {"done": False}

    def banner(text):
        try:
            win.evaluate_js(BANNER_JS % json.dumps(text))
        except Exception:
            pass

    def watch():
        time.sleep(3)
        if close_after:
            time.sleep(close_after)
            win.destroy()
            return
        banner(f"Đăng nhập {s['label']} như bình thường. Đăng nhập xong cửa sổ sẽ tự đóng.")
        while not state["done"]:
            try:
                cookies = win.get_cookies() or []
            except Exception:
                cookies = []
            names = {n for ck in cookies for n in ck.keys()}
            if any(a in names for a in s["auth"]):
                # land on the site itself so every cookie of the session is visible, then save
                try:
                    if site_of_url(win.get_current_url() or "") != site:
                        win.load_url(HOME[site])
                        time.sleep(4)
                        cookies = win.get_cookies() or cookies
                except Exception:
                    pass
                save_cookies(site, cookies)
                state["done"] = True
                banner(f"✓ Đã đăng nhập {s['label']}. Auto Clipper sẽ nhớ phiên này. Cửa sổ tự đóng...")
                time.sleep(2.5)
                win.destroy()
                return
            time.sleep(2)

    threading.Thread(target=watch, daemon=True).start()
    webview.start(private_mode=False, storage_path=storage)
    return 0 if state["done"] else 2
