"""Robust video downloader (yt-dlp) with live progress, stall watchdog, retries and browser-login fallback.

One yt-dlp run gets the video, its info and every subtitle track worth having; the best track is chosen
afterwards from the info. Fragments download in parallel. If YouTube asks for a login, the run is repeated
with the user's own browser session (only when the user enabled that in Settings)."""
import glob, json, os, re, subprocess, threading, time

STALL_SECONDS = 120          # no output for this long -> kill and retry
PROGRESS_RE = re.compile(r"\[dl\]\s*([\d.]+)%\s*(\S+)\s+ETA\s+(\S+)")


class DownloadError(Exception):
    """Message for the user."""


def _explain(text):
    s = text.lower()
    if "sign in to confirm" in s or "not a bot" in s:
        return ("YouTube yêu cầu đăng nhập để tải video này. Vào Cài đặt → Tải video, chọn trình duyệt "
                "bạn đang đăng nhập YouTube (Firefox ổn định nhất; với Chrome/Edge hãy đóng trình duyệt trước), rồi thử lại.")
    if "age" in s and ("restrict" in s or "confirm your age" in s):
        return "Video giới hạn độ tuổi: chọn trình duyệt đã đăng nhập YouTube trong Cài đặt rồi thử lại."
    if "private video" in s or "members-only" in s or "join this channel" in s:
        return "Video riêng tư hoặc chỉ dành cho hội viên, không tải được."
    if "video unavailable" in s or "has been removed" in s or "not available in your country" in s:
        return "Video không còn tồn tại, bị chặn ở quốc gia của bạn, hoặc link sai."
    if "is not a valid url" in s or "unsupported url" in s:
        return "Link không hợp lệ hoặc trang này không được hỗ trợ."
    if "could not copy" in s and "cookie" in s:
        return "Không đọc được đăng nhập từ trình duyệt: hãy đóng hẳn trình duyệt đó rồi thử lại (hoặc chọn Firefox)."
    if "http error 403" in s:
        return "YouTube từ chối tải (403). Thử lại sau ít phút, hoặc bật dùng đăng nhập trình duyệt trong Cài đặt."
    if "timed out" in s or "connection" in s or "getaddrinfo" in s or "network" in s:
        return "Lỗi mạng khi tải video. Kiểm tra kết nối Internet rồi thử lại."
    tail = "\n".join(text.strip().splitlines()[-2:])
    return f"Không tải được video.\n{tail}"


def _needs_login(text):
    s = text.lower()
    return "sign in to confirm" in s or "not a bot" in s or ("age" in s and "restrict" in s)


class Downloader:
    def __init__(self, ytdlp, deno, ffmpeg_dir, log, progress, cancelled):
        self.ytdlp, self.deno, self.ffmpeg_dir = ytdlp, deno, ffmpeg_dir
        self.log, self.progress, self.cancelled = log, progress, cancelled
        self.proc = None

    def _args(self, url, out_dir, max_height, browser):
        H = max_height
        fmt = (f"bv*[height<={H}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={H}][ext=mp4]+ba[ext=m4a]/"
               f"bv*[height<={H}]+ba/b[height<={H}]/bv*+ba/b")
        a = [self.ytdlp, "--no-playlist", "--newline", "--no-colors", "--ignore-config",
             "--js-runtimes", f"deno:{self.deno}", "--ffmpeg-location", self.ffmpeg_dir,
             "-f", fmt, "--merge-output-format", "mp4",
             "-N", "4", "--retries", "10", "--fragment-retries", "10", "--socket-timeout", "30",
             "--write-info-json", "--no-write-playlist-metafiles",
             "--write-subs", "--write-auto-subs", "--sub-langs", r"vi,en,vi-orig,en-orig,.*-orig",
             "--sub-format", "vtt/srt/best", "--ignore-errors",
             "--progress-template", "download:[dl] %(progress._percent_str)s %(progress._speed_str)s ETA %(progress._eta_str)s",
             "-o", os.path.join(out_dir, "source.%(ext)s"), url]
        if browser:
            a[1:1] = ["--cookies-from-browser", browser]
        return a

    def _run_once(self, args):
        """Run yt-dlp, stream its output; returns (exit code, collected text)."""
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                     bufsize=1)
        lines, last = [], [time.time()]
        stage = {"n": 0}

        def reader():
            for line in self.proc.stdout:
                last[0] = time.time()
                line = line.rstrip()
                if not line:
                    continue
                lines.append(line)
                m = PROGRESS_RE.search(line)
                if m:
                    pct = float(m.group(1))
                    if pct < 1 and stage["n"] == 0:
                        stage["n"] = 1
                    # video then audio stream: show the bigger one as the main bar
                    self.progress(pct / 100, f"Đang tải video... {pct:.0f}% · {m.group(2)} · còn {m.group(3)}")
                elif "[Merger]" in line or "Merging formats" in line:
                    self.progress(1.0, "Đang ghép hình và tiếng...")
                    self.log(line)
                elif line.startswith(("[youtube]", "[info]", "[download] Destination", "WARNING", "ERROR")) or \
                        "Writing video subtitles" in line:
                    self.log(line)
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        while self.proc.poll() is None:
            if self.cancelled():
                self.proc.kill()
                break
            if time.time() - last[0] > STALL_SECONDS:
                self.log(f"Không có phản hồi trong {STALL_SECONDS} giây, đang thử lại...")
                self.proc.kill()
                t.join(5)
                return -9, "\n".join(lines)
            time.sleep(0.3)
        t.join(5)
        return self.proc.returncode, "\n".join(lines)

    def download(self, url, out_dir, max_height=1080, browser=None):
        """Returns (video_path, subtitle_path_or_None, info dict)."""
        os.makedirs(out_dir, exist_ok=True)
        attempts = [browser] if browser else [None]
        tried_stall = False
        i = 0
        while i < len(attempts):
            b = attempts[i]
            if b:
                self.log(f"Thử lại bằng đăng nhập YouTube từ {b}...")
            self.progress(0, "Đang lấy thông tin video...")
            code, text = self._run_once(self._args(url, out_dir, max_height, b))
            if self.cancelled():
                raise DownloadError("Đã huỷ.")
            video = os.path.join(out_dir, "source.mp4")
            # source.mp4 only appears once video+audio are merged; --ignore-errors keeps a failed
            # subtitle request from failing the whole run, so judge by the file, not the exit code
            if os.path.exists(video) and os.path.getsize(video) > 0:
                return video, *self._pick_subs(out_dir)
            if code == -9 and not tried_stall:
                tried_stall = True
                continue  # one retry after a stall
            i += 1
            if i >= len(attempts):
                raise DownloadError(_explain(text))
        raise DownloadError("Không tải được video.")

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    @staticmethod
    def _pick_subs(out_dir):
        info = {}
        ij = os.path.join(out_dir, "source.info.json")
        if os.path.exists(ij):
            try:
                j = json.load(open(ij, encoding="utf-8"))
                info = {"title": j.get("title"), "channel": j.get("channel") or j.get("uploader"),
                        "duration": j.get("duration"), "language": j.get("language"),
                        "description": (j.get("description") or "")[:3000],
                        "chapters": [{"title": c.get("title"), "start": c.get("start_time")}
                                     for c in j.get("chapters") or []]}
            except Exception:
                pass
        lang = (info.get("language") or "").split("-")[0]
        subs = {}
        for p in glob.glob(os.path.join(out_dir, "source.*.vtt")) + glob.glob(os.path.join(out_dir, "source.*.srt")):
            code = os.path.basename(p)[len("source."):].rsplit(".", 1)[0]
            subs[code] = p
        order = []
        if lang:
            order += [lang, f"{lang}-orig"]          # creator subs, then YouTube's speech recognition
        order += [c for c in subs if c.endswith("-orig")]
        if not lang and len(subs) == 1:
            order += list(subs)                       # language unknown, single track: use it
        for code in order:
            if code in subs:
                info["subtitle_kind"] = "auto" if code.endswith("-orig") else "manual"
                return subs[code], info
        return None, info
