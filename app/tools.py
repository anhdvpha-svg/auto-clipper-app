"""External tools the app needs: yt-dlp (downloads), ffmpeg/ffprobe (video), deno (yt-dlp's JS runtime).

They are downloaded on first run into the app's data folder instead of being bundled, so the installer
stays small and yt-dlp can keep itself up to date (YouTube changes often)."""
import io, json, os, platform, shutil, stat, subprocess, sys, time, zipfile

import httpx

from . import config

GH = "https://github.com"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WIN else ""


def tools_dir():
    d = os.path.join(config.data_dir(), "tools")
    os.makedirs(d, exist_ok=True)
    return d


def _paths():
    t = tools_dir()
    return {
        "yt-dlp": os.path.join(t, "yt-dlp" + EXE),
        "ffmpeg": os.path.join(t, "ffmpeg" + EXE),
        "ffprobe": os.path.join(t, "ffprobe" + EXE),
        "deno": os.path.join(t, "deno" + EXE),
    }


def _download(url, log, label):
    log(f"Đang tải {label}...")
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        buf = io.BytesIO()
        last = 0
        for chunk in r.iter_bytes(1 << 20):
            buf.write(chunk)
            if total and time.time() - last > 1.5:
                last = time.time()
                log(f"  {label}: {buf.tell() * 100 // total}% ({buf.tell() >> 20}/{total >> 20} MB)")
    return buf.getvalue()


def _make_exec(p):
    if not IS_WIN:
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if IS_MAC:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", p], capture_output=True)


def _extract(data, names, dest):
    """Pull the named files (by basename) out of a zip into dest."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename)
            if base in names:
                with z.open(info) as src, open(os.path.join(dest, base), "wb") as out:
                    shutil.copyfileobj(src, out)
                _make_exec(os.path.join(dest, base))


def _sources():
    arch = platform.machine().lower()
    if IS_WIN:
        return {
            "yt-dlp": (f"{GH}/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe", None),
            "ffmpeg": (f"{GH}/yt-dlp/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
                       ["ffmpeg.exe", "ffprobe.exe"]),
            "deno": (f"{GH}/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip", ["deno.exe"]),
        }
    if IS_MAC:
        darch = "aarch64" if arch in ("arm64", "aarch64") else "x86_64"
        return {
            "yt-dlp": (f"{GH}/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos", None),
            "ffmpeg": ("https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip", ["ffmpeg"]),
            "ffprobe": ("https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip", ["ffprobe"]),
            "deno": (f"{GH}/denoland/deno/releases/latest/download/deno-{darch}-apple-darwin.zip", ["deno"]),
        }
    darch = "aarch64" if arch in ("arm64", "aarch64") else "x86_64"
    return {
        "yt-dlp": (f"{GH}/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux", None),
        "deno": (f"{GH}/denoland/deno/releases/latest/download/deno-{darch}-unknown-linux-gnu.zip", ["deno"]),
    }


def ensure(log=print):
    """Make sure every tool exists; return their paths and put them on PATH for the engine."""
    paths = _paths()
    found = {}
    for name in ("ffmpeg", "ffprobe"):
        sys_bin = shutil.which(name)
        if os.path.exists(paths[name]):
            found[name] = paths[name]
        elif sys_bin and not IS_WIN:
            found[name] = sys_bin  # Linux/mac dev machines usually have ffmpeg installed
    for name, (url, members) in _sources().items():
        if name in found or os.path.exists(paths[name]):
            continue
        data = _download(url, log, name)
        if members is None:
            with open(paths[name], "wb") as f:
                f.write(data)
            _make_exec(paths[name])
        else:
            _extract(data, members, tools_dir())
    for name, p in paths.items():
        if name not in found and os.path.exists(p):
            found[name] = p
    missing = [n for n in ("yt-dlp", "ffmpeg", "ffprobe", "deno") if n not in found]
    if missing:
        raise RuntimeError("Thiếu công cụ: " + ", ".join(missing))

    # engine scripts call `ffmpeg`/`ffprobe` by name and read these variables
    bindirs = {os.path.dirname(found["ffmpeg"]), tools_dir()}
    os.environ["PATH"] = os.pathsep.join([*bindirs, os.environ.get("PATH", "")])
    os.environ["AUTOCLIPPER_YTDLP"] = found["yt-dlp"]
    os.environ["AUTOCLIPPER_DENO"] = found["deno"]
    os.environ["AUTOCLIPPER_FONTS"] = config.resource_path("assets", "fonts")
    _maybe_update_ytdlp(found["yt-dlp"], log)
    return found


def _maybe_update_ytdlp(bin_path, log):
    stamp = os.path.join(tools_dir(), "last_update.json")
    try:
        last = json.load(open(stamp)).get("ytdlp", 0)
    except Exception:
        last = 0
    if time.time() - last < 3 * 86400:
        return
    log("Đang kiểm tra bản cập nhật yt-dlp...")
    try:
        subprocess.run([bin_path, "-U"], capture_output=True, timeout=120)
    except Exception:
        pass
    json.dump({"ytdlp": time.time()}, open(stamp, "w"))
