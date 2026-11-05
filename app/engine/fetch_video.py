#!/usr/bin/env python3
"""Download a video from a YouTube (or other yt-dlp supported) link, plus its best subtitle track.

Usage:
  python fetch_video.py URL --out-dir clipper_work
Options:
  --max-height 1080          cap the video resolution (default 1080)
  --lang vi                  preferred subtitle language (default: the video's own language)
  --no-subs                  don't fetch subtitles (Whisper will transcribe instead)
  --cookies-from-browser chrome   use the browser's YouTube login (age-restricted / "confirm you're not a bot")

Writes to --out-dir:
  source.mp4          the video
  source.<lang>.vtt   subtitles, if a usable track exists (manual subs preferred over auto-captions)
  source_info.json    title, channel, duration, language, chapters, description, which subtitle was used
Prints the next command to run (transcribe.py with or without --srt).
"""
import argparse, glob, json, os, subprocess, sys


FROZEN = getattr(sys, "frozen", False)  # running inside the packaged desktop app
YTDLP_BIN = os.environ.get("AUTOCLIPPER_YTDLP")  # the app ships/updates its own yt-dlp executable


def ensure_ytdlp():
    if YTDLP_BIN or FROZEN:
        return
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        print("yt-dlp not found, installing it (pip install -U yt-dlp)...", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"], check=True)


JS_RUNTIME = []


def ensure_deno():
    """yt-dlp needs a JavaScript runtime (Deno) to get YouTube's full format list; without it
    downloads can fail or come back low-res. The `deno` PyPI package ships the binary."""
    global JS_RUNTIME
    if os.environ.get("AUTOCLIPPER_DENO"):
        JS_RUNTIME = ["--js-runtimes", "deno:" + os.environ["AUTOCLIPPER_DENO"]]
        return
    if FROZEN:
        return
    try:
        import deno
    except ImportError:
        print("Installing Deno (JavaScript runtime yt-dlp needs for YouTube)...", file=sys.stderr)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "deno"], capture_output=True)
        if r.returncode:
            return
        import importlib
        deno = importlib.import_module("deno")
    try:
        JS_RUNTIME = ["--js-runtimes", f"deno:{deno.find_deno_bin()}"]
    except Exception:
        JS_RUNTIME = []


def ytdlp(args, capture=True):
    base = [YTDLP_BIN] if YTDLP_BIN else [sys.executable, "-m", "yt_dlp"]
    return subprocess.run([*base, "--no-playlist", "--no-warnings", *JS_RUNTIME, *args],
                          capture_output=capture, text=True, encoding="utf-8", errors="replace")


def explain_failure(stderr):
    """Turn yt-dlp's error into a short, actionable message."""
    s = stderr.lower()
    tail = "\n".join(stderr.strip().splitlines()[-3:])
    if "sign in to confirm" in s or "not a bot" in s:
        hint = ("YouTube is asking for a login (bot check). Re-run with --cookies-from-browser chrome "
                "(or firefox/edge/safari) on a machine where you're signed in to YouTube.")
    elif "age" in s and "restrict" in s or "inappropriate" in s:
        hint = "The video is age-restricted. Re-run with --cookies-from-browser chrome while signed in."
    elif "private video" in s or "members-only" in s or "join this channel" in s:
        hint = "The video is private or members-only; it can't be downloaded without access."
    elif "unavailable" in s or "not available" in s or "removed" in s:
        hint = "The video is unavailable (deleted, region-blocked, or the link is wrong)."
    elif "unable to download video data" in s and "403" in s:
        hint = ("YouTube refused to send the video file to this machine (HTTP 403). This is normal on cloud "
                "servers and some VPNs; it works from a home/office connection.")
    elif ("403" in s or "proxy" in s or "unable to download" in s or "connection" in s
          or "name or service not known" in s or "getaddrinfo" in s or "timed out" in s):
        hint = ("Network problem reaching the site (blocked by a firewall/proxy or offline). "
                "Ask the user to upload the video file instead, or allow youtube.com/googlevideo.com.")
    elif "ffmpeg" in s:
        hint = "ffmpeg is needed to merge video and audio. Install it (apt/brew/winget install ffmpeg)."
    else:
        hint = "Download failed. Try `pip install -U yt-dlp` (YouTube changes often) and run again."
    return f"{hint}\n--- yt-dlp said ---\n{tail}"


def installed_browsers():
    """Browsers with a profile on THIS machine, in the order worth trying for YouTube cookies.
    On a cloud/server sandbox this is normally empty."""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    mac = os.path.join(home, "Library", "Application Support")
    candidates = {
        "chrome": [os.path.join(local, "Google", "Chrome", "User Data"), os.path.join(mac, "Google", "Chrome"),
                   os.path.join(home, ".config", "google-chrome")],
        "edge": [os.path.join(local, "Microsoft", "Edge", "User Data"), os.path.join(mac, "Microsoft Edge"),
                 os.path.join(home, ".config", "microsoft-edge")],
        "firefox": [os.path.join(roaming, "Mozilla", "Firefox"), os.path.join(mac, "Firefox"),
                    os.path.join(home, ".mozilla", "firefox")],
        "brave": [os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
                  os.path.join(mac, "BraveSoftware", "Brave-Browser"),
                  os.path.join(home, ".config", "BraveSoftware", "Brave-Browser")],
        "safari": [os.path.join(home, "Library", "Cookies")],
        "chromium": [os.path.join(home, ".config", "chromium"), os.path.join(mac, "Chromium")],
    }
    return [b for b, paths in candidates.items() if any(p and os.path.isdir(p) for p in paths)]


NO_BROWSER_HELP = """NEXT_STEP: This machine has no web browser profile, so it is a cloud/server sandbox.
YouTube blocks downloads from cloud servers ("confirm you're not a bot") and this cannot be fixed from here.
Do NOT try to work around the bot check. Offer the user these options instead:
  1. Easiest: run the one-click downloader on their own computer
     (helpers/tai_video_windows.bat or helpers/tai_video_mac.command in this skill - send them the file),
     paste the link, then attach the downloaded video (and the .vtt/.srt subtitle next to it) here.
  2. Their own channel: download the MP4 from YouTube Studio > Content > (video) > Download.
  3. Fully automatic: use this skill in Claude Code on their own computer, where links download directly."""


def lang_matches(code, lang):
    return code == lang or code.startswith(lang + "-")


def choose_subtitle(info, preferred=None):
    """Return (code, kind) for the best subtitle track, or (None, None).

    Order: manual subs in the preferred/original language, then YouTube's own speech recognition
    in the original language ("<lang>-orig" or "<lang>" when it is the video's language).
    Auto-captions in *other* languages are machine translations of the ASR - skip them, a wrong
    language or a translation is worse for clipping than running Whisper.
    """
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    original = (info.get("language") or "").split("-")[0] or None
    wanted = [l for l in [preferred, original] if l]
    if not wanted:
        wanted = ["vi", "en"]

    for lang in wanted:
        for code in sorted(manual, key=len):
            if lang_matches(code, lang):
                return code, "manual"
    # no original language known but exactly one manual track: use it
    if not original and len(manual) == 1:
        return next(iter(manual)), "manual"

    for lang in wanted:
        if f"{lang}-orig" in auto:
            return f"{lang}-orig", "auto"
    # any "-orig" track is the real ASR of the spoken language
    for code in auto:
        if code.endswith("-orig"):
            return code, "auto"
    # a plain code is only trustworthy when it is the video's own language; otherwise it may be
    # a machine translation, and Whisper on the real audio is the better choice
    if original and original in auto:
        return original, "auto"
    return None, None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--max-height", type=int, default=1080)
    ap.add_argument("--lang", help="preferred subtitle language code, e.g. vi or en")
    ap.add_argument("--no-subs", action="store_true")
    ap.add_argument("--cookies-from-browser",
                    help="chrome, firefox, edge, safari, brave ... or 'auto' to try the browsers on this computer")
    a = ap.parse_args(argv)
    os.makedirs(a.out_dir, exist_ok=True)
    ensure_ytdlp()
    ensure_deno()
    browsers = installed_browsers()
    if a.cookies_from_browser == "auto":
        tries = [["--cookies-from-browser", b] for b in browsers] or [[]]
    elif a.cookies_from_browser:
        tries = [["--cookies-from-browser", a.cookies_from_browser]]
    else:
        tries = [[]]

    # 1. metadata only - fast, and tells us which subtitle tracks exist
    common, r = [], None
    for opt in tries:
        r = ytdlp([*opt, "-J", a.url])
        if r.returncode == 0 and r.stdout.strip():
            common = opt
            if opt:
                print(f"Using YouTube login from {opt[1]}", file=sys.stderr)
            break
    if r.returncode != 0 or not r.stdout.strip():
        msg = "Could not read the video page.\n" + explain_failure(r.stderr)
        bot = "not a bot" in r.stderr.lower() or "sign in to confirm" in r.stderr.lower()
        if bot and not browsers:
            msg += "\n\n" + NO_BROWSER_HELP
        elif bot and not a.cookies_from_browser:
            msg += ("\n\nNEXT_STEP: this computer has " + ", ".join(browsers) + ". Ask the user once whether "
                    "to use their YouTube login from the browser, then re-run with --cookies-from-browser auto.")
        sys.exit(msg)
    info = json.loads(r.stdout)
    if info.get("_type") == "playlist":
        sys.exit("This link is a playlist/channel. Send a link to a single video.")
    if info.get("is_live") or info.get("live_status") == "is_live":
        sys.exit("This is a live stream that is still running; wait until it ends, then try again.")

    sub_code, sub_kind = (None, None) if a.no_subs else choose_subtitle(info, a.lang)

    # 2. download video (+ the chosen subtitle track) in one go
    H = a.max_height
    # prefer H.264 (avc1): AV1/VP9 decode 1.5-3x slower in every later step (analysis and rendering)
    fmt = (f"bv*[height<={H}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={H}][ext=mp4]+ba[ext=m4a]/"
           f"bv*[height<={H}]+ba/b[height<={H}]/bv*+ba/b")
    out_tmpl = os.path.join(a.out_dir, "source.%(ext)s")
    args = [*common, "-f", fmt, "--merge-output-format", "mp4", "-o", out_tmpl, "--newline"]
    if sub_code:
        args += ["--write-subs" if sub_kind == "manual" else "--write-auto-subs",
                 "--sub-langs", sub_code, "--sub-format", "vtt/srt/best"]
    print(f"Downloading: {info.get('title')} ({int(info.get('duration') or 0) // 60} min)...", file=sys.stderr)
    r = ytdlp([*args, a.url], capture=False) if sys.stderr.isatty() else ytdlp([*args, a.url])
    video = os.path.join(a.out_dir, "source.mp4")
    if r.returncode != 0 or not os.path.exists(video):
        msg = "Video download failed.\n" + explain_failure(getattr(r, "stderr", "") or "")
        if not browsers:
            msg += "\n\n" + NO_BROWSER_HELP
        sys.exit(msg)

    subs = sorted(glob.glob(os.path.join(a.out_dir, "source.*.vtt")) +
                  glob.glob(os.path.join(a.out_dir, "source.*.srt")))
    sub_file = subs[0] if subs else None
    if sub_code and not sub_file:
        print(f"Note: subtitle track {sub_code} could not be downloaded; Whisper will be needed.",
              file=sys.stderr)
        sub_kind = None

    meta = {
        "url": a.url,
        "id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "language": info.get("language"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "chapters": [{"title": c.get("title"), "start": c.get("start_time"), "end": c.get("end_time")}
                     for c in (info.get("chapters") or [])],
        "description": (info.get("description") or "")[:3000],
        "video_file": os.path.abspath(video),
        "subtitle_file": os.path.abspath(sub_file) if sub_file else None,
        "subtitle_kind": sub_kind,  # "manual" (uploaded by creator), "auto" (YouTube ASR) or None
        "subtitle_lang": sub_code if sub_file else None,
    }
    with open(os.path.join(a.out_dir, "source_info.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    print(f"Saved {video}")
    print(f"Info  {os.path.join(a.out_dir, 'source_info.json')}  "
          f"({len(meta['chapters'])} chapters)")
    scripts = os.path.dirname(os.path.abspath(__file__))
    if sub_file:
        print(f"Subs  {sub_file}  ({sub_kind}, {sub_code})")
        print("Next:", f"python {scripts}/transcribe.py {video} --out-dir {a.out_dir} --srt {sub_file}")
    else:
        print("Subs  none usable - transcribe with Whisper")
        print("Next:", f"python {scripts}/transcribe.py {video} --out-dir {a.out_dir}")


if __name__ == "__main__":
    main()
