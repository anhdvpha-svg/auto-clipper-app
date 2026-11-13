"""Watch a source video inside the app (no browser): yt-dlp finds the stream, ffmpeg decodes pictures and sound,
pictures are drawn on a Tk canvas and sound goes to the speakers through sounddevice; the sound card is the clock.
The window also shows the post's caption / description (copyable) and can fetch the spoken words."""
import collections, json, os, re, subprocess, tempfile, threading, time, webbrowser

import customtkinter as ctk

FPS = 25
SR = 44100
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class PlayerError(Exception):
    pass


def _cookie_header(s):
    """yt-dlp's per-format 'cookies' string -> a Cookie header value."""
    if not s:
        return ""
    attrs = {"domain", "path", "expires", "max-age", "secure", "httponly", "samesite"}
    pairs = []
    for part in s.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip().lower() not in attrs:
                pairs.append(f"{k.strip()}={v.strip()}")
    return "; ".join(dict.fromkeys(pairs))


def _stream(fmt):
    h = dict(fmt.get("http_headers") or {})
    ck = _cookie_header(fmt.get("cookies"))
    if ck:
        h["Cookie"] = ck
    return {"url": fmt.get("url"), "headers": h, "proto": fmt.get("protocol") or ""}


def resolve(url, ytdlp, deno=None, cookies=None, browser=None):
    """Metadata + playable stream(s) at <= 480p (enough for a preview, quick to start)."""
    if os.path.isfile(url):
        from .engine.render_clips import ffprobe_json
        j = ffprobe_json(url)
        v = next((x for x in j["streams"] if x["codec_type"] == "video"), {})
        has_a = any(x["codec_type"] == "audio" for x in j["streams"])
        return {"title": os.path.basename(url), "video": {"url": url, "headers": {}, "proto": "file"},
                "audio": {"url": url, "headers": {}, "proto": "file"} if has_a else None, "description": "",
                "tags": [], "webpage_url": url, "duration": float(j["format"].get("duration") or 0) or None,
                "width": v.get("width"), "height": v.get("height")}
    from . import platform_login
    a = [ytdlp, "-J", "--no-playlist", "--no-warnings", "--ignore-config",
         "-f", "b[height<=480][vcodec!=none][acodec!=none]/bv*[height<=480]+ba/b[height<=720]/bv*+ba/b"]
    if deno:
        a += ["--js-runtimes", f"deno:{deno}"]
    ck = cookies or platform_login.cookies_for_url(url)
    if ck:
        a += ["--cookies", ck]
    elif browser:
        a += ["--cookies-from-browser", browser]
    r = subprocess.run(a + [url], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       stdin=subprocess.DEVNULL, timeout=120, creationflags=_NO_WINDOW)
    out = r.stdout.strip()
    if not out:
        err = (r.stderr.strip().splitlines() or ["không rõ lỗi"])[-1]
        from .downloader import _explain
        raise PlayerError(_explain(err, url))
    d = json.loads(out)
    if d.get("_type") == "playlist" and d.get("entries"):
        d = d["entries"][0]
    rf = d.get("requested_formats")
    if rf:
        vid = next((f for f in rf if f.get("vcodec") != "none"), rf[0])
        aud = next((f for f in rf if f.get("acodec") != "none"), None)
    else:
        vid = aud = d
    return {"title": d.get("title") or "", "description": d.get("description") or "", "tags": d.get("tags") or [],
            "uploader": d.get("uploader") or d.get("channel") or "", "duration": d.get("duration"),
            "view_count": d.get("view_count"), "like_count": d.get("like_count"),
            "webpage_url": d.get("webpage_url") or url, "width": vid.get("width") or d.get("width"),
            "height": vid.get("height") or d.get("height"), "video": _stream(vid),
            "audio": _stream(aud) if aud else None, "subtitles": d.get("subtitles") or {},
            "automatic_captions": d.get("automatic_captions") or {}, "language": d.get("language")}


def caption_text(info):
    """The post's own words: title, description and hashtags - what a clipper copies."""
    parts = []
    t, desc = (info.get("title") or "").strip(), (info.get("description") or "").strip()
    if t and not desc.startswith(t[:40]):
        parts.append(t)
    if desc:
        parts.append(desc)
    tags = [x for x in info.get("tags") or [] if x]
    found = set(re.findall(r"#\w+", desc))
    extra = ["#" + re.sub(r"\s+", "", x) for x in tags if "#" + re.sub(r"\s+", "", x) not in found][:15]
    if extra:
        parts.append(" ".join(extra))
    return "\n\n".join(parts)


def hashtags_of(text):
    return " ".join(dict.fromkeys(re.findall(r"#\w+", text)))


def spoken_text(info):
    """Words said in the video, from the platform's subtitles (creator's first, else automatic)."""
    import httpx
    lang = (info.get("language") or "").split("-")[0]
    for pool in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        keys = list(pool)
        order = [k for k in (lang, f"{lang}-orig", "vi", "en", "vi-orig", "en-orig") if k and k in pool]
        order += [k for k in keys if k.endswith("-orig")] + keys
        for k in order:
            tr = next((t for t in pool[k] if t.get("ext") == "vtt"), None) or \
                next((t for t in pool[k] if t.get("ext") in ("srt", "vtt")), None)
            if not tr:
                continue
            try:
                r = httpx.get(tr["url"], timeout=30, follow_redirects=True)
                r.raise_for_status()
            except Exception:
                continue
            from .engine import transcribe
            fd, path = tempfile.mkstemp(suffix="." + tr.get("ext", "vtt"))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(r.text)
            try:
                _, segs = transcribe.from_subtitles(path)
            finally:
                os.remove(path)
            text = " ".join(s["text"] for s in segs)
            if text.strip():
                return text, k
    return "", None


class Player:
    """Decodes one stream pair with ffmpeg; frames are pulled by the UI at the pace of the audio clock."""

    def __init__(self, info, box_w, box_h):
        self.info, self.W, self.H = info, box_w, box_h
        self.procs, self.frames = [], collections.deque(maxlen=FPS * 3)
        self.pos, self.playing, self.volume, self.ended = 0.0, False, 1.0, False
        self._t0, self._samples, self._stream, self._gen, self.first = 0.0, 0, None, 0, True

    def _input(self, s, start):
        a = ["-ss", f"{start:.2f}"] if start > 0.05 else []
        if s["proto"] != "file":
            hdr = "".join(f"{k}: {v}\r\n" for k, v in (s["headers"] or {}).items())
            if hdr:
                a += ["-headers", hdr]
            if s["proto"].startswith("http") and "m3u8" not in s["proto"]:
                a += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4"]
        return a + ["-i", s["url"]]

    def start(self, at=None):
        self.stop()
        self._gen += 1
        gen = self._gen
        self.pos = self.pos if at is None else max(0.0, at)
        self.frames.clear()
        self.ended, self.playing, self._samples, self.first = False, True, 0, True
        vf = (f"fps={FPS},scale={self.W}:{self.H}:force_original_aspect_ratio=decrease,"
              f"pad={self.W}:{self.H}:(ow-iw)/2:(oh-ih)/2:color=black")
        v = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", *self._input(self.info["video"], self.pos),
                              "-an", "-sn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             creationflags=_NO_WINDOW)
        self.procs.append(v)
        threading.Thread(target=self._read_video, args=(v, gen), daemon=True).start()
        self._t0 = time.time()
        self._stream = None
        if self.info.get("audio"):
            a = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", *self._input(self.info["audio"], self.pos),
                                  "-vn", "-ac", "2", "-ar", str(SR), "-f", "s16le", "-"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                 creationflags=_NO_WINDOW)
            self.procs.append(a)
            self._start_audio(a, gen)

    def _read_video(self, proc, gen):
        size = self.W * self.H * 3
        n = 0
        while gen == self._gen:
            buf = proc.stdout.read(size)
            if not buf or len(buf) < size:
                break
            while len(self.frames) >= self.frames.maxlen and gen == self._gen:
                time.sleep(0.01)  # the screen is behind: wait instead of dropping decoded frames
            self.frames.append((self.pos + n / FPS, buf))
            n += 1
        if gen == self._gen:
            self.ended = True

    def _start_audio(self, proc, gen):
        try:
            import numpy as np
            import sounddevice as sd
        except Exception:
            return  # no sound library: the wall clock drives the pictures
        buf = bytearray()
        lock = threading.Lock()

        def feed():
            while gen == self._gen:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                with lock:
                    buf.extend(chunk)
                while len(buf) > SR * 4 * 4 and gen == self._gen:
                    time.sleep(0.02)

        def callback(outdata, frames, _t, _status):
            need = frames * 4
            with lock:
                data = bytes(buf[:need])
                del buf[:need]
            if len(data) < need:
                data += b"\0" * (need - len(data))
            else:
                self._samples += frames
            if self.volume != 1.0:
                arr = (np.frombuffer(data, dtype=np.int16) * self.volume).astype(np.int16)
                data = arr.tobytes()
            outdata[:] = data
        threading.Thread(target=feed, daemon=True).start()
        try:
            self._stream = sd.RawOutputStream(samplerate=SR, channels=2, dtype="int16", callback=callback,
                                              blocksize=1024)
            self._stream.start()
        except Exception:
            self._stream = None

    def clock(self):
        if not self.playing:
            return self.pos
        if self._samples:
            return self.pos + self._samples / SR
        if self._stream is not None and time.time() - self._t0 < 4:
            return self.pos  # sound still buffering: hold the first picture
        return self.pos + (time.time() - self._t0)  # no sound (or none after 4 s): the wall clock

    def frame(self):
        """Newest frame due by the clock (older ones are skipped); the very first one is shown at once."""
        now = self.clock()
        out = None
        while self.frames and self.frames[0][0] <= now + 0.02:
            out = self.frames.popleft()
        if out is None and self.first and self.frames:
            out = self.frames[0]
        if out is not None:
            self.first = False
        return out

    def pause(self):
        if self.playing:
            self.pos = self.clock()
            self.stop()

    def stop(self):
        self._gen += 1
        self.playing = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        for p in self.procs:
            try:
                p.kill()
            except Exception:
                pass
        self.procs = []


def _fmt(t):
    t = max(0, int(t or 0))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


class PlayerWindow(ctk.CTkToplevel):
    """Video + caption panel. `item` is a research result / saved link ({"url", "title", ...})."""

    def __init__(self, app, item):
        super().__init__(app)
        self.app, self.item, self.T = app, item, app.theme
        T, f = self.T, app.font
        self.title(item.get("title") or "Xem video")
        self.configure(fg_color=T["BG"])
        self.geometry("1180x720")
        self.minsize(980, 620)
        self.transient(app)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.info, self.player, self._photo, self._seek_job, self._dragging = None, None, None, None, False
        self.vertical = any(k in item.get("platform", "") for k in ("Shorts", "TikTok", "Instagram")) or \
            "/shorts/" in item.get("url", "")
        self._layout_video()
        side = ctk.CTkFrame(self, fg_color=T["CARD"], corner_radius=16, border_width=1, border_color=T["BORDER"])
        side.grid(row=0, column=1, sticky="nsew", padx=(9, 18), pady=18)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(3, weight=1)
        self.ttl = ctk.CTkLabel(side, text=item.get("title", ""), font=f(16, "bold"), text_color=T["TEXT"],
                                anchor="w", justify="left", wraplength=470)
        self.ttl.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 2))
        self.meta = ctk.CTkLabel(side, text=item.get("platform", ""), font=f(12), text_color=T["MUTED"], anchor="w")
        self.meta.grid(row=1, column=0, sticky="ew", padx=16)
        tabs = ctk.CTkSegmentedButton(side, values=["Caption / mô tả", "Lời thoại"], font=f(13),
                                      selected_color=T["ACCENT"], selected_hover_color=T["ACCENT_HOVER"],
                                      unselected_color=T["CARD2"], fg_color=T["CARD2"], text_color=T["TEXT"],
                                      command=self._tab)
        tabs.set("Caption / mô tả")
        tabs.grid(row=2, column=0, sticky="w", padx=16, pady=(12, 6))
        self.tabs = tabs
        self.text = ctk.CTkTextbox(side, fg_color=T["CARD2"], text_color=T["TEXT"], font=f(13), corner_radius=10,
                                   wrap="word")
        self.text.grid(row=3, column=0, sticky="nsew", padx=16)
        self.text.insert("1.0", "Đang lấy caption...")
        cb = ctk.CTkFrame(side, fg_color="transparent")
        cb.grid(row=4, column=0, sticky="ew", padx=16, pady=(8, 4))
        app.button(cb, "Copy caption", self._copy_all, width=120, height=34).grid(row=0, column=0, padx=(0, 6))
        app.button(cb, "Copy hashtag", self._copy_tags, width=120, height=34).grid(row=0, column=1, padx=(0, 6))
        app.button(cb, "Copy link", lambda: self._copy(item["url"], "link"), width=100, height=34).grid(row=0, column=2)
        act = ctk.CTkFrame(side, fg_color="transparent")
        act.grid(row=5, column=0, sticky="ew", padx=16, pady=(4, 6))
        app.button(act, "🔖 Lưu link", lambda: app.save_link_menu(self._item_full(), self), width=110,
                   height=38).grid(row=0, column=0, padx=(0, 6))
        app.button(act, "Mở trên web", lambda: webbrowser.open(item["url"]), width=110, height=38).grid(
            row=0, column=1, padx=(0, 6))
        app.button(act, "Cắt ngay  →", self._cut, primary=True, width=140, height=38).grid(row=0, column=2)
        self.msg = ctk.CTkLabel(side, text="", font=f(12), text_color=T["MUTED"], anchor="w", justify="left",
                                wraplength=470)
        self.msg.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 12))
        self.spoken = None
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(50, self.lift)
        threading.Thread(target=self._load, daemon=True).start()
        self.after(30, self._tick)

    # ---------- video area ----------
    def _layout_video(self):
        T, f = self.T, self.app.font
        self.VW, self.VH = (338, 600) if self.vertical else (640, 360)
        left = ctk.CTkFrame(self, fg_color=T["CARD"], corner_radius=16, border_width=1, border_color=T["BORDER"])
        left.grid(row=0, column=0, sticky="ns", padx=(18, 9), pady=18)
        self.left = left
        import tkinter as tk
        self.canvas = tk.Canvas(left, width=self.VW, height=self.VH, bg="#000000", highlightthickness=0)
        self.canvas.grid(row=0, column=0, columnspan=4, padx=16, pady=(16, 8))
        self.canvas.create_text(self.VW / 2, self.VH / 2, text="Đang tải video...", fill="#9CA0AB",
                                font=("Segoe UI", 12), tags="msg")
        self.canvas.bind("<Button-1>", lambda _e: self._toggle())
        self.play_btn = self.app.button(left, "⏸", self._toggle, width=44, height=34)
        self.play_btn.grid(row=1, column=0, padx=(16, 6), pady=(0, 14))
        self.seek = ctk.CTkSlider(left, from_=0, to=1, progress_color=T["ACCENT"], button_color=T["TEXT"],
                                  button_hover_color=T["TEXT"], command=self._seeking, width=self.VW - 170)
        self.seek.set(0)
        self.seek.grid(row=1, column=1, sticky="ew", pady=(0, 14))
        self.time_lbl = ctk.CTkLabel(left, text="0:00", font=f(12), text_color=T["MUTED"], width=90)
        self.time_lbl.grid(row=1, column=2, pady=(0, 14))
        self.mute_btn = self.app.button(left, "🔊", self._mute, width=40, height=34)
        self.mute_btn.grid(row=1, column=3, padx=(0, 16), pady=(0, 14))

    def _load(self):
        from . import tools
        try:
            if os.path.isfile(self.item["url"]):
                tools.use_installed()
                t = {"yt-dlp": None, "deno": None}
            else:
                t = tools.ensure(lambda m: None)
            info = resolve(self.item["url"], t["yt-dlp"], t["deno"], self.app.cfg.get("cookies_file") or None,
                           self.app.cfg.get("browser") or None)
            self.after(0, lambda: self._loaded(info, None))
        except Exception as e:
            self.after(0, lambda e=e: self._loaded(None, str(e)))

    def _loaded(self, info, err):
        if not self.winfo_exists():
            return
        if err:
            self.canvas.delete("msg")
            self.canvas.create_text(self.VW / 2, self.VH / 2, text="Không phát được video:\n\n" + err[:400],
                                    fill="#F87171", width=self.VW - 40, font=("Segoe UI", 11), tags="msg")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", self.item.get("title", ""))
            self._login_hint(err)
            return
        self.info = info
        w, h = info.get("width") or 16, info.get("height") or 9
        if (h > w) != self.vertical:  # wrong guess of the shape: rebuild the video area
            self.vertical = h > w
            self.left.destroy()
            self._layout_video()
        if info.get("duration"):
            self.seek.configure(to=max(1, info["duration"]))
        self.text.delete("1.0", "end")
        self.text.insert("1.0", caption_text(info) or "(Video này không có caption / mô tả)")
        bits = [self.item.get("platform", ""), info.get("uploader", "")]
        if info.get("view_count"):
            from .research import human_views
            bits.append(f"{human_views(info['view_count'])} view")
        if info.get("like_count"):
            from .research import human_views
            bits.append(f"{human_views(info['like_count'])} thích")
        self.meta.configure(text=" · ".join(b for b in bits if b))
        if info.get("title") and not self.item.get("title"):
            self.ttl.configure(text=info["title"])
        self.player = Player(info, self.VW, self.VH)
        self.player.start(0)

    def _login_hint(self, err):
        from . import platform_login
        site = platform_login.site_of_url(self.item.get("url", ""))
        if site and ("đăng nhập" in err.lower()):
            self.msg.configure(text=f"Cần đăng nhập {platform_login.SITES[site]['label']}.", text_color="#FBBF24")
            self.app.button(self.left, f"🔒 Đăng nhập {platform_login.SITES[site]['label']}",
                            lambda: self.app.login_site(site, after=self._retry), primary=True, height=38).grid(
                row=2, column=0, columnspan=4, sticky="ew", padx=16, pady=(0, 14))

    def _retry(self):
        self.canvas.delete("all")
        self.canvas.create_text(self.VW / 2, self.VH / 2, text="Đang tải video...", fill="#9CA0AB", tags="msg")
        threading.Thread(target=self._load, daemon=True).start()

    def _tick(self):
        if not self.winfo_exists():
            return
        p = self.player
        if p:
            fr = p.frame()
            if fr:
                from PIL import Image, ImageTk
                im = Image.frombuffer("RGB", (self.VW, self.VH), fr[1], "raw", "RGB", 0, 1)
                self._photo = ImageTk.PhotoImage(im)
                self.canvas.delete("all")
                self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
            now = p.clock()
            if not self._dragging:
                self.seek.set(now)
            self.time_lbl.configure(text=f"{_fmt(now)} / {_fmt(self.info.get('duration'))}")
            if p.ended and not p.frames and p.playing:
                p.pause()
                self.play_btn.configure(text="▶")
        self.after(15, self._tick)

    def _toggle(self):
        p = self.player
        if not p:
            return
        if p.playing:
            p.pause()
            self.play_btn.configure(text="▶")
        else:
            if p.ended:
                p.pos = 0.0
            p.start()
            self.play_btn.configure(text="⏸")

    def _seeking(self, v):
        self._dragging = True
        self.time_lbl.configure(text=f"{_fmt(v)} / {_fmt((self.info or {}).get('duration'))}")
        if self._seek_job:
            self.after_cancel(self._seek_job)
        self._seek_job = self.after(250, lambda: self._seek_to(v))

    def _seek_to(self, v):
        self._seek_job, self._dragging = None, False
        if self.player:
            self.player.start(float(v))
            self.play_btn.configure(text="⏸")

    def _mute(self):
        if self.player:
            self.player.volume = 0.0 if self.player.volume else 1.0
            self.mute_btn.configure(text="🔇" if not self.player.volume else "🔊")

    # ---------- caption panel ----------
    def _tab(self, which):
        self.text.delete("1.0", "end")
        if which == "Lời thoại":
            if self.spoken is None:
                self.text.insert("1.0", "Đang lấy lời thoại (phụ đề của video)...")
                threading.Thread(target=self._load_spoken, daemon=True).start()
            else:
                self.text.insert("1.0", self.spoken or "Video này không có phụ đề / lời thoại trên nền tảng.")
        else:
            self.text.insert("1.0", caption_text(self.info or {}) or self.item.get("title", ""))

    def _load_spoken(self):
        try:
            txt, lang = spoken_text(self.info or {})
        except Exception:
            txt, lang = "", None
        self.spoken = txt

        def show():
            if self.winfo_exists() and self.tabs.get() == "Lời thoại":
                self.text.delete("1.0", "end")
                self.text.insert("1.0", txt or "Video này không có phụ đề / lời thoại trên nền tảng.")
                if lang:
                    self.msg.configure(text=f"Lời thoại từ phụ đề ({lang}).", text_color=self.T["MUTED"])
        self.after(0, show)

    def _copy(self, text, what):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.msg.configure(text=f"Đã copy {what}.", text_color="#4ADE80")

    def _copy_all(self):
        self._copy(self.text.get("1.0", "end").strip(), "caption" if self.tabs.get() != "Lời thoại" else "lời thoại")

    def _copy_tags(self):
        tags = hashtags_of(caption_text(self.info or {}))
        self._copy(tags, "hashtag") if tags else self.msg.configure(text="Video không có hashtag.",
                                                                   text_color=self.T["MUTED"])

    def _item_full(self):
        it = dict(self.item)
        if self.info:
            it["caption"] = caption_text(self.info)
            it.setdefault("title", self.info.get("title"))
        return it

    def _cut(self):
        self._close()
        self.app.enqueue([self.item["url"]], front=True)

    def _close(self):
        if self.player:
            self.player.stop()
        self.destroy()
