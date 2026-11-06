"""Auto Clipper desktop app - CapCut-style UI: paste a link or pick a file, get vertical clips."""
import glob, json, os, queue, subprocess, sys, threading, time, webbrowser

import customtkinter as ctk

from . import config, llm
from .pipeline import Job, is_url

APP_TITLE = "Auto Clipper"
VERSION = "1.6"

# ---- palette (dark, cyan accent - CapCut desktop feel) ----
BG = "#0E0F12"
SIDEBAR = "#141519"
CARD = "#1B1C21"
CARD2 = "#24252C"
BORDER = "#2E3038"
TEXT = "#F3F4F6"
MUTED = "#9CA0AB"
ACCENT = "#22D3EE"
ACCENT_HOVER = "#0FB8D2"
ACCENT_TEXT = "#03161A"
DANGER = "#F87171"
OK = "#4ADE80"

PROVIDERS = [("offline", "Offline"), ("claude", "Claude"), ("openai", "ChatGPT"), ("gemini", "Gemini"),
             ("custom", "API tuỳ chỉnh")]
KEY_LINKS = {"claude": "https://console.anthropic.com/settings/keys",
             "openai": "https://platform.openai.com/api-keys",
             "gemini": "https://aistudio.google.com/apikey"}
NUMS = [("0", "Tự động"), ("3", "3"), ("5", "5"), ("8", "8"), ("10", "10")]
MAX_CLIPS = 30
LENGTHS = [("15-30", "15–30s"), ("30-60", "30–60s"), ("60-90", "60–90s")]
ZOOMFX = [("off", "Tắt"), ("light", "Nhẹ"), ("medium", "Vừa"), ("strong", "Mạnh")]
FRAMINGS = [("auto", "Tự động"), ("center", "Cắt giữa"), ("blur", "Nền mờ")]
LANGS = [("auto", "Theo video"), ("vi", "Tiếng Việt"), ("en", "English")]
ENCODERS = [("auto", "Nhanh nhất (GPU nếu có)"), ("x264", "Chỉ dùng CPU")]
BROWSERS = [("", "Không dùng"), ("firefox", "Firefox"), ("chrome", "Chrome"), ("edge", "Edge"), ("brave", "Brave")]
COLORS = ["FFE600", "22D3EE", "FF4FA3", "5BFF6B", "FFFFFF"]
STEPS = ["Tải video", "Chép lời", "Phân tích hình ảnh", "Chọn đoạn hay", "Lên khung từng cảnh", "Dựng clip"]
STEP_AT = [0.0, 0.15, 0.30, 0.45, 0.55, 0.65]


def open_path(p):
    if os.name == "nt":
        os.startfile(p)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def reveal(path):
    """Open the file manager with this file selected."""
    path = os.path.normpath(path)
    if os.name == "nt":
        subprocess.Popen(f'explorer /select,"{path}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def dir_size(path):
    total = 0
    for dp, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]


def label_of(pairs, key):
    return dict(pairs).get(key, pairs[0][1])


def key_of(pairs, label):
    return next((k for k, v in pairs if v == label), pairs[0][0])


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.cfg = config.load()
        self.F = self._font_family()
        self.title(APP_TITLE)
        self.geometry("1280x820")
        self.minsize(1080, 700)
        self.configure(fg_color=BG)
        try:
            ico = config.resource_path("assets", "icon.ico")
            if os.name == "nt" and os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass
        self.theme = {"BG": BG, "CARD": CARD, "CARD2": CARD2, "BORDER": BORDER, "TEXT": TEXT, "MUTED": MUTED,
                      "ACCENT": ACCENT, "ACCENT_HOVER": ACCENT_HOVER}
        self.open_path = open_path
        self.q = queue.Queue()
        self.job = None
        self.last_result = None
        self._thumbs = []
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self.pages = {}
        self._build_create()
        self._build_projects()
        self._build_settings()
        self.show("create")
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- helpers ----------------
    def _font_family(self):
        try:
            import tkinter.font as tkfont
            fams = set(tkfont.families(self))
        except Exception:
            fams = set()
        for f in ("Segoe UI Variable Display", "Segoe UI", "Inter", "SF Pro Display", "Helvetica Neue"):
            if f in fams:
                return f
        return None

    def font(self, size=13, weight="normal"):
        return ctk.CTkFont(family=self.F, size=size, weight=weight) if self.F else ctk.CTkFont(size=size, weight=weight)

    def card(self, master, **kw):
        return ctk.CTkFrame(master, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER, **kw)

    def h(self, master, text, size=15):
        return ctk.CTkLabel(master, text=text, font=self.font(size, "bold"), text_color=TEXT, anchor="w")

    def muted(self, master, text, size=12, **kw):
        return ctk.CTkLabel(master, text=text, font=self.font(size), text_color=MUTED, anchor="w", justify="left", **kw)

    def seg(self, master, pairs, value, command=None):
        s = ctk.CTkSegmentedButton(master, values=[v for _, v in pairs], font=self.font(13), height=34,
                                   fg_color=CARD2, unselected_color=CARD2, unselected_hover_color=BORDER,
                                   selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
                                   text_color=TEXT, corner_radius=10, command=command)
        s.set(label_of(pairs, value))
        return s

    def button(self, master, text, command, primary=False, **kw):
        if primary:
            return ctk.CTkButton(master, text=text, command=command, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                 text_color=ACCENT_TEXT, font=self.font(15, "bold"), corner_radius=12, **kw)
        return ctk.CTkButton(master, text=text, command=command, fg_color=CARD2, hover_color=BORDER,
                             text_color=TEXT, font=self.font(13), corner_radius=10, border_width=1,
                             border_color=BORDER, **kw)

    # ---------------- sidebar ----------------
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, fg_color=SIDEBAR, corner_radius=0, width=220)
        sb.grid(row=0, column=0, sticky="ns")
        sb.grid_propagate(False)
        sb.grid_rowconfigure(10, weight=1)
        logo = ctk.CTkFrame(sb, fg_color="transparent")
        logo.grid(row=0, column=0, sticky="ew", padx=18, pady=(22, 26))
        try:
            from PIL import Image
            im = Image.open(config.resource_path("assets", "icon.png"))
            self._logo = ctk.CTkImage(im, im, size=(34, 34))
            ctk.CTkLabel(logo, image=self._logo, text="").grid(row=0, column=0, padx=(0, 10))
        except Exception:
            pass
        ctk.CTkLabel(logo, text="Auto Clipper", font=self.font(18, "bold"), text_color=TEXT).grid(row=0, column=1)
        self.nav = {}
        for i, (key, text) in enumerate((("create", "✂   Tạo clip"), ("projects", "▦   Dự án"),
                                         ("settings", "⚙   Cài đặt")), start=1):
            b = ctk.CTkButton(sb, text=text, anchor="w", height=42, corner_radius=10, font=self.font(14),
                              fg_color="transparent", hover_color=CARD2, text_color=MUTED,
                              command=lambda k=key: self.show(k))
            b.grid(row=i, column=0, sticky="ew", padx=12, pady=3)
            self.nav[key] = b
        self.muted(sb, f"Phiên bản {VERSION}\nXử lý ngay trên máy bạn", 11).grid(row=11, column=0, sticky="w",
                                                                                   padx=20, pady=18)

    def show(self, key):
        for k, page in self.pages.items():
            page.grid_remove() if k != key else page.grid(row=0, column=1, sticky="nsew")
        for k, b in self.nav.items():
            b.configure(fg_color=CARD2 if k == key else "transparent", text_color=TEXT if k == key else MUTED)
        if key == "projects":
            self._refresh_projects()

    # ---------------- create page ----------------
    def _build_create(self):
        page = ctk.CTkScrollableFrame(self, fg_color=BG, corner_radius=0)
        page.grid_columnconfigure(0, weight=1)
        self.pages["create"] = page
        wrap = ctk.CTkFrame(page, fg_color="transparent")
        wrap.grid(row=0, column=0, sticky="ew", padx=36, pady=(30, 30))
        wrap.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(wrap, text="Biến video dài thành clip ngắn", font=self.font(28, "bold"),
                     text_color=TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        self.muted(wrap, "Dán link hoặc tải file lên. AI chọn đoạn hay, tự đổi khung 9:16, gắn phụ đề và tiêu đề hook.",
                   14).grid(row=1, column=0, sticky="w", pady=(4, 18))

        # input bar
        bar = self.card(wrap)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.src = ctk.CTkEntry(bar, height=52, corner_radius=12, border_width=0, fg_color=CARD2,
                                font=self.font(15), text_color=TEXT,
                                placeholder_text="Dán link YouTube, TikTok, Facebook...", placeholder_text_color=MUTED)
        self.src.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=12)
        self.src.bind("<Return>", lambda _: self._start())
        self.button(bar, "Tải file lên", self._pick_file, height=52, width=140).grid(row=0, column=1, padx=(0, 8))
        self.go = self.button(bar, "Tạo clip  →", self._start, primary=True, height=52, width=160)
        self.go.grid(row=0, column=2, padx=(0, 12))

        # options
        opt = self.card(wrap)
        opt.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        opt.grid_columnconfigure((0, 1), weight=1, uniform="opt")

        def row(r, c, title, widget, span=1):
            self.muted(opt, title, 12).grid(row=r * 2, column=c, sticky="w", padx=18, pady=(14 if r == 0 else 6, 4))
            widget.grid(row=r * 2 + 1, column=c, columnspan=span, sticky="ew", padx=18, pady=(0, 8))

        self.provider = self.seg(opt, PROVIDERS, self.cfg.get("provider", "offline"), lambda _: self._provider_hint())
        row(0, 0, "AI chọn đoạn", self.provider)
        numbox = ctk.CTkFrame(opt, fg_color="transparent")
        numbox.grid_columnconfigure(0, weight=1)
        saved_n = int(self.cfg.get("num_clips", 0) or 0)
        preset = str(saved_n) in dict(NUMS)
        self.num = self.seg(numbox, NUMS, str(saved_n) if preset else "0", lambda _: self._num_preset())
        if not preset:
            self.num.set("")
        self.num.grid(row=0, column=0, sticky="ew")
        self.num_custom = ctk.CTkEntry(numbox, width=74, height=34, corner_radius=10, border_width=1,
                                       border_color=BORDER, fg_color=CARD2, font=self.font(13), text_color=TEXT,
                                       justify="center", placeholder_text="Số khác", placeholder_text_color=MUTED)
        if not preset:
            self.num_custom.insert(0, str(saved_n))
        self.num_custom.grid(row=0, column=1, padx=(8, 0))
        self.num_custom.bind("<KeyRelease>", lambda _: self._num_typed())
        row(0, 1, "Số clip", numbox)
        self.length = self.seg(opt, LENGTHS, self.cfg.get("length", "30-60"))
        row(1, 0, "Độ dài mỗi clip", self.length)
        self.framing = self.seg(opt, FRAMINGS, self.cfg.get("framing", "auto"))
        row(1, 1, "Khung dọc 9:16", self.framing)
        self.topic = ctk.CTkEntry(opt, height=38, corner_radius=10, border_width=0, fg_color=CARD2, font=self.font(13),
                                  text_color=TEXT, placeholder_text="Ví dụ: những đoạn hài hước, đoạn nói về marketing "
                                                                    "(để trống = đoạn hay nhất)",
                                  placeholder_text_color=MUTED)
        if self.cfg.get("topic"):
            self.topic.insert(0, self.cfg["topic"])
        row(2, 0, "Chỉ lấy đoạn về (không bắt buộc)", self.topic, span=2)

        style = ctk.CTkFrame(opt, fg_color="transparent")
        row(3, 0, "Phong cách phụ đề", style, span=2)
        self.color = self.cfg.get("highlight", "FFE600")
        self.swatches = {}
        for i, c in enumerate(COLORS):
            b = ctk.CTkButton(style, text="", width=28, height=28, corner_radius=14, fg_color=f"#{c}",
                              hover_color=f"#{c}", border_width=3, command=lambda c=c: self._pick_color(c))
            b.grid(row=0, column=i, padx=(0, 8))
            self.swatches[c] = b
        self._pick_color(self.color)
        self.cap = self._switch(style, "Phụ đề", self.cfg.get("captions", True), 6)
        self.upper = self._switch(style, "IN HOA", self.cfg.get("uppercase", True), 7)
        self.hook = self._switch(style, "Tiêu đề hook", self.cfg.get("hook", True), 8)
        fxbox = ctk.CTkFrame(opt, fg_color="transparent")
        self.zoomfx = self.seg(fxbox, ZOOMFX, self.cfg.get("zoom_fx", "medium"))
        self.zoomfx.grid(row=0, column=0, sticky="w")
        self.muted(fxbox, "Tự nghe nhạc: zoom giật vào điểm nhấn (tiếng nổ, trống), nhấp theo beat, "
                          "đẩy zoom chậm ở cảnh dài.", 12).grid(row=0, column=1, sticky="w", padx=(12, 0))
        row(4, 0, "Zoom theo nhịp nhạc", fxbox, span=2)
        self.hint = self.muted(opt, "", 12)
        self.hint.grid(row=12, column=0, columnspan=2, sticky="w", padx=18, pady=(2, 14))
        self._provider_hint()

        # progress card (hidden until a job runs)
        self.prog = self.card(wrap)
        self.prog.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(self.prog, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 6))
        top.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(top, text="", font=self.font(16, "bold"), text_color=TEXT, anchor="w", justify="left", wraplength=640)
        self.status.grid(row=0, column=0, sticky="w")
        self.pct = ctk.CTkLabel(top, text="0%", font=self.font(16, "bold"), text_color=ACCENT)
        self.pct.grid(row=0, column=1, padx=10)
        self.stop = self.button(top, "Huỷ", self._cancel, width=80, height=32)
        self.stop.grid(row=0, column=2)
        self.bar = ctk.CTkProgressBar(self.prog, height=8, corner_radius=4, progress_color=ACCENT, fg_color=CARD2)
        self.bar.set(0)
        self.bar.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 12))
        steps = ctk.CTkFrame(self.prog, fg_color="transparent")
        steps.grid(row=2, column=0, sticky="ew", padx=14)
        self.step_lbl = []
        for i, s in enumerate(STEPS):
            l = ctk.CTkLabel(steps, text=f"○  {s}", font=self.font(13), text_color=MUTED)
            l.grid(row=0, column=i, padx=6, pady=2, sticky="w")
            self.step_lbl.append(l)
        det = ctk.CTkFrame(self.prog, fg_color="transparent")
        det.grid(row=3, column=0, sticky="ew", padx=20, pady=(6, 14))
        self.det_btn = ctk.CTkButton(det, text="Chi tiết ▾", width=90, height=26, fg_color="transparent",
                                     hover_color=CARD2, text_color=MUTED, font=self.font(12), command=self._toggle_log)
        self.det_btn.grid(row=0, column=0, sticky="w")
        self.logbox = ctk.CTkTextbox(self.prog, height=220, fg_color="#0B0C0F", corner_radius=10, text_color="#C9CDD6",
                                     font=ctk.CTkFont(family="Consolas" if os.name == "nt" else "monospace", size=12))
        self.log_visible = False

        # results
        self.res = ctk.CTkFrame(wrap, fg_color="transparent")
        self.res.grid_columnconfigure(0, weight=1)
        self.wrap = wrap

    def _num_preset(self):
        self.num_custom.delete(0, "end")
        self.num_custom.configure(border_color=BORDER)

    def _num_typed(self):
        t = self.num_custom.get().strip()
        if not t:
            self.num.set("Tự động")
            self.num_custom.configure(border_color=BORDER)
            return
        ok = t.isdigit() and 1 <= int(t) <= MAX_CLIPS
        self.num_custom.configure(border_color=ACCENT if ok else DANGER)
        if ok:
            self.num.set("")  # the typed number wins: no preset highlighted

    def _num_value(self):
        t = self.num_custom.get().strip()
        if t:
            if not t.isdigit() or not 1 <= int(t) <= MAX_CLIPS:
                raise ValueError(f"Số clip phải là số từ 1 đến {MAX_CLIPS}.")
            return int(t)
        return int(key_of(NUMS, self.num.get())) if self.num.get() else 0

    def _switch(self, master, text, on, col):
        s = ctk.CTkSwitch(master, text=text, font=self.font(13), text_color=TEXT, progress_color=ACCENT,
                          button_color=TEXT, button_hover_color=TEXT, fg_color=BORDER)
        (s.select if on else s.deselect)()
        s.grid(row=0, column=col, padx=(18 if col == 6 else 10, 0))
        return s

    def _pick_color(self, c):
        self.color = c
        for k, b in self.swatches.items():
            b.configure(border_color=TEXT if k == c else CARD)

    def _provider_hint(self):
        p = key_of(PROVIDERS, self.provider.get())
        if p == "offline":
            self.hint.configure(text="Offline: miễn phí, chọn đoạn theo âm lượng, chuyển động và câu mở đầu. "
                                     "Dùng AI để có đoạn chọn, hook và caption hay hơn.", text_color=MUTED)
        elif p == "custom" and not (self.cfg.get("base_urls") or {}).get("custom"):
            self.hint.configure(text="Chưa nhập địa chỉ API tuỳ chỉnh — vào Cài đặt → Tài khoản AI.", text_color=DANGER)
        elif not config.get_key(self.cfg, p):
            self.hint.configure(text=f"Chưa có API key {label_of(PROVIDERS, p)} — vào Cài đặt để thêm.",
                                text_color=DANGER)
        else:
            m = self.cfg["models"].get(p) or "(chưa chọn model)"
            self.hint.configure(text=f"Dùng {label_of(PROVIDERS, p)} · model {m} · trả phí theo lượt dùng.",
                                text_color=MUTED)

    def _toggle_log(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.logbox.grid(row=4, column=0, sticky="ew", padx=20, pady=(0, 16))
            self.det_btn.configure(text="Chi tiết ▴")
        else:
            self.logbox.grid_remove()
            self.det_btn.configure(text="Chi tiết ▾")

    def _set_step(self, frac, failed=False):
        cur = max(i for i, a in enumerate(STEP_AT) if frac >= a)
        for i, l in enumerate(self.step_lbl):
            if frac >= 1.0 or i < cur:
                l.configure(text=f"✓  {STEPS[i]}", text_color=OK)
            elif i == cur:
                l.configure(text=f"{'✕' if failed else '●'}  {STEPS[i]}", text_color=DANGER if failed else ACCENT)
            else:
                l.configure(text=f"○  {STEPS[i]}", text_color=MUTED)

    # ---------------- results ----------------
    def _thumb(self, path, score, length, w=168):
        from PIL import Image, ImageDraw, ImageFont, ImageOps
        h = int(w * 16 / 9)
        try:
            im = ImageOps.fit(Image.open(path).convert("RGB"), (w * 2, h * 2))
        except Exception:
            im = Image.new("RGB", (w * 2, h * 2), CARD2)
        d = ImageDraw.Draw(im)
        try:
            f = ImageFont.truetype(config.resource_path("assets", "fonts", "Inter-ExtraBold.otf"), 30)
        except Exception:
            f = ImageFont.load_default()
        badge = str(score)
        bw = int(d.textlength(badge, font=f)) + 28
        H2 = h * 2
        d.rounded_rectangle([14, H2 - 58, 14 + bw, H2 - 14], radius=14, fill=ACCENT)
        d.text((28, H2 - 54), badge, font=f, fill=ACCENT_TEXT)
        dur = f"{int(length) // 60}:{int(length) % 60:02d}"
        tw = int(d.textlength(dur, font=f)) + 24
        d.rounded_rectangle([w * 2 - 14 - tw, h * 2 - 58, w * 2 - 14, h * 2 - 14], radius=12, fill=(0, 0, 0))
        d.text((w * 2 - 14 - tw + 12, h * 2 - 54), dur, font=f, fill=(255, 255, 255))
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=28, fill=255)
        im = im.convert("RGBA")
        im.putalpha(mask)
        img = ctk.CTkImage(im, im, size=(w, h))
        self._thumbs.append(img)
        return img

    def _show_results(self, res):
        for w in self.res.winfo_children():
            w.destroy()
        self._thumbs = []
        self.last_result = res
        head = ctk.CTkFrame(self.res, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(22, 10))
        head.grid_columnconfigure(0, weight=1)
        self.h(head, f"Kết quả · {len(res['clips'])} clip", 20).grid(row=0, column=0, sticky="w")
        self.button(head, "Mở thư mục", lambda: open_path(res["out_dir"]), height=36, width=120).grid(row=0, column=1)
        grid = ctk.CTkFrame(self.res, fg_color="transparent")
        grid.grid(row=1, column=0, sticky="ew")
        cols = 4
        for c in range(cols):
            grid.grid_columnconfigure(c, weight=1, uniform="clips")
        clips = sorted(res["clips"], key=lambda c: -c["score"]["total"])
        for i, c in enumerate(clips):
            card = ctk.CTkFrame(grid, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
            card.grid(row=i // cols, column=i % cols, sticky="nsew", padx=6, pady=6)
            card.grid_columnconfigure(0, weight=1)
            img = self._thumb(c["thumb"], c["score"]["total"], c["length"])
            pic = ctk.CTkButton(card, image=img, text="", fg_color="transparent", hover_color=CARD2,
                                command=lambda p=c["file"]: open_path(p))
            pic.grid(row=0, column=0, padx=10, pady=(10, 6))
            ctk.CTkLabel(card, text=c["title"], font=self.font(13, "bold"), text_color=TEXT, wraplength=190,
                         justify="left", anchor="w").grid(row=1, column=0, sticky="ew", padx=14)
            if c.get("hook_title"):
                self.muted(card, f"Hook: {c['hook_title']}", 11, wraplength=190).grid(row=2, column=0, sticky="ew",
                                                                                     padx=14, pady=(2, 0))
            ctk.CTkButton(card, text="▶  Xem clip", height=32, corner_radius=8, fg_color=ACCENT,
                          hover_color=ACCENT_HOVER, text_color=ACCENT_TEXT, font=self.font(12, "bold"),
                          command=lambda p=c["file"]: open_path(p)).grid(row=3, column=0, sticky="ew", padx=10,
                                                                         pady=(8, 4))
            btns = ctk.CTkFrame(card, fg_color="transparent")
            btns.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 12))
            btns.grid_columnconfigure((0, 1, 2), weight=1, uniform="b")
            self.button(btns, "✎ Sửa", lambda i=c.get("index", i): self._edit(res["out_dir"], i), height=30, width=50
                        ).grid(row=0, column=0, sticky="ew", padx=(0, 3))
            cap = (c.get("captions") or {}).get("TikTok") or c["title"]
            tags = " ".join(c.get("hashtags") or [])
            self.button(btns, "Copy", lambda t=f"{cap}\n{tags}".strip(): self._copy(t), height=30, width=50
                        ).grid(row=0, column=1, sticky="ew", padx=3)
            self.button(btns, "Vị trí", lambda p=c["file"]: reveal(p), height=30, width=50
                        ).grid(row=0, column=2, sticky="ew", padx=(3, 0))
        self.res.grid(row=5, column=0, sticky="ew")

    def _edit(self, out_dir, index):
        from .editor import ClipEditor, ProjectError
        try:
            ClipEditor(self, out_dir, index, self._on_edited)
        except ProjectError as e:
            self.status.configure(text=str(e), text_color=DANGER)
        except Exception as e:
            self.status.configure(text=f"Không mở được trình chỉnh sửa: {e}", text_color=DANGER)

    def _on_edited(self, index, clip, rendered):
        res = self.last_result
        if not res:
            return
        d = res["out_dir"]
        for c in res["clips"]:
            if c.get("index") == index:
                c.update(clip)
                c.update(file=os.path.join(d, rendered["file"]), thumb=os.path.join(d, rendered["thumb"]),
                         length=rendered["length"])
        self._show_results(res)

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.configure(text="Đã copy caption vào bộ nhớ tạm.")

    # ---------------- projects page ----------------
    def _build_projects(self):
        page = ctk.CTkScrollableFrame(self, fg_color=BG, corner_radius=0)
        page.grid_columnconfigure(0, weight=1)
        self.pages["projects"] = page
        head = ctk.CTkFrame(page, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=36, pady=(30, 4))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Dự án", font=self.font(28, "bold"), text_color=TEXT, anchor="w").grid(
            row=0, column=0, sticky="w")
        self.button(head, "Mở thư mục lưu", lambda: open_path(self._out_root()), height=36, width=140).grid(row=0, column=1)
        self.proj_info = self.muted(page, "", 14)
        self.proj_info.grid(row=1, column=0, sticky="w", padx=36)
        self.proj_search = ctk.CTkEntry(page, height=40, corner_radius=10, border_width=0, fg_color=CARD2,
                                        font=self.font(14), text_color=TEXT, placeholder_text="Tìm theo tên video...",
                                        placeholder_text_color=MUTED)
        self.proj_search.grid(row=2, column=0, sticky="ew", padx=36, pady=(14, 4))
        self.proj_search.bind("<KeyRelease>", lambda _: self._render_projects())
        self.proj_list = ctk.CTkFrame(page, fg_color="transparent")
        self.proj_list.grid(row=3, column=0, sticky="ew", padx=30, pady=(8, 24))
        self.proj_list.grid_columnconfigure(0, weight=1)
        self._projects = []
        self._confirm = None

    def _out_root(self):
        r = self.cfg.get("output_dir") or config.default_output_dir()
        os.makedirs(r, exist_ok=True)
        return r

    def _refresh_projects(self):
        """Scan <Lưu vào>/<YYYY-MM-DD>/<project>/ (and older flat folders)."""
        root = self._out_root()
        found = glob.glob(os.path.join(root, "*", "*", "clips_rendered.json")) + \
            glob.glob(os.path.join(root, "*", "clips_rendered.json"))
        items = []
        for p in found:
            d = os.path.dirname(p)
            try:
                data = json.load(open(p, encoding="utf-8"))
                proj = json.load(open(os.path.join(d, "project.json"), encoding="utf-8")) \
                    if os.path.exists(os.path.join(d, "project.json")) else {}
            except Exception:
                continue
            mtime = os.path.getmtime(p)
            created = proj.get("created")
            try:
                when = time.mktime(time.strptime(created, "%Y-%m-%dT%H:%M:%S")) if created else mtime
            except ValueError:
                when = mtime
            work = os.path.join(d, "_work")
            big = [os.path.join(work, f) for f in os.listdir(work)] if os.path.isdir(work) else []
            big = [f for f in big if f.lower().endswith((".mp4", ".webm", ".mkv", ".m4a", ".part"))]
            items.append({"dir": d, "data": data, "proj": proj, "when": when, "size": dir_size(d),
                          "temp": sum(os.path.getsize(f) for f in big), "temp_files": big,
                          "name": proj.get("title") or os.path.basename(d)})
        items.sort(key=lambda x: -x["when"])
        self._projects = items
        total = sum(x["size"] for x in items)
        self.proj_info.configure(text=f"{len(items)} dự án · {human(total)} trên ổ đĩa · lưu tại {root}")
        self._confirm = None
        self._render_projects()

    def _render_projects(self):
        for w in self.proj_list.winfo_children():
            w.destroy()
        q = self.proj_search.get().strip().lower()
        items = [x for x in self._projects if q in x["name"].lower()] if q else self._projects
        if not items:
            self.muted(self.proj_list, "Chưa có dự án nào." if not q else "Không có dự án khớp tìm kiếm.", 14).grid(
                row=0, column=0, sticky="w", padx=6, pady=20)
            return
        self._proj_thumbs = []
        row_i, last_day = 0, None
        for it in items[:200]:
            day = time.strftime("%Y-%m-%d", time.localtime(it["when"]))
            if day != last_day:
                last_day = day
                lt = time.localtime(it["when"])
                same = [x for x in items if time.strftime("%Y-%m-%d", time.localtime(x["when"])) == day]
                hdr = ctk.CTkFrame(self.proj_list, fg_color="transparent")
                hdr.grid(row=row_i, column=0, sticky="ew", padx=6, pady=(16 if row_i else 4, 4))
                hdr.grid_columnconfigure(0, weight=1)
                today = time.strftime("%Y-%m-%d")
                title = "Hôm nay" if day == today else f"{WEEKDAYS[lt.tm_wday]}, {time.strftime('%d/%m/%Y', lt)}"
                self.h(hdr, f"{title}  ·  {len(same)} dự án  ·  {human(sum(x['size'] for x in same))}", 15).grid(
                    row=0, column=0, sticky="w")
                day_dir = os.path.dirname(it["dir"])
                self.button(hdr, "Mở thư mục ngày", lambda dd=day_dir: open_path(dd), height=30, width=130).grid(
                    row=0, column=1)
                row_i += 1
            self._project_row(it, row_i)
            row_i += 1

    def _project_row(self, it, r):
        d, data = it["dir"], it["data"]
        row = self.card(self.proj_list)
        row.grid(row=r, column=0, sticky="ew", pady=4)
        row.grid_columnconfigure(1, weight=1)
        thumb = os.path.join(d, data["rendered"][0]["thumb"]) if data.get("rendered") else ""
        try:
            from PIL import Image, ImageOps
            im = ImageOps.fit(Image.open(thumb).convert("RGB"), (54, 96))
            img = ctk.CTkImage(im, im, size=(54, 96))
            self._proj_thumbs.append(img)
            ctk.CTkLabel(row, image=img, text="").grid(row=0, column=0, rowspan=3, padx=14, pady=12)
        except Exception:
            pass
        name = ctk.CTkLabel(row, text=it["name"], font=self.font(15, "bold"), text_color=TEXT, anchor="w",
                            wraplength=520, justify="left")
        name.grid(row=0, column=1, sticky="sw", pady=(14, 0))
        when = time.strftime("%H:%M", time.localtime(it["when"]))
        extra = f" · file tạm {human(it['temp'])}" if it["temp"] else ""
        self.muted(row, f"{when} · {len(data.get('rendered', []))} clip · {human(it['size'])}{extra}", 12).grid(
            row=1, column=1, sticky="nw")
        status = self.muted(row, "", 12)
        status.grid(row=2, column=1, sticky="nw", pady=(0, 10))
        res = {"out_dir": d, "clips": [{**c, "index": k, "file": os.path.join(d, r2["file"]),
                                        "thumb": os.path.join(d, r2["thumb"]), "length": r2["length"]}
                                       for k, (c, r2) in enumerate(zip(data["clips"], data["rendered"]))]}
        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.grid(row=0, column=2, rowspan=3, padx=(6, 14))
        self.button(btns, "Xem clip", lambda r_=res: (self._show_results(r_), self.show("create")), width=90,
                    height=32).grid(row=0, column=0, padx=3)
        self.button(btns, "Mở thư mục", lambda: open_path(d), width=100, height=32).grid(row=0, column=1, padx=3)
        self.button(btns, "Đổi tên", lambda: self._rename_project(it, name, status), width=80, height=32).grid(
            row=0, column=2, padx=3)
        if it["temp"]:
            self.button(btns, "Dọn file tạm", lambda: self._clean_project(it, status), width=110, height=32).grid(
                row=0, column=3, padx=3)
        delb = ctk.CTkButton(btns, text="Xoá", width=70, height=32, corner_radius=10, fg_color=CARD2,
                             hover_color="#5B1F1F", text_color=DANGER, font=self.font(13), border_width=1,
                             border_color=BORDER)
        delb.configure(command=lambda: self._delete_project(it, delb, status))
        delb.grid(row=0, column=4, padx=3)

    def _delete_project(self, it, btn, status):
        import shutil
        if self._confirm is not it:  # first click arms, second click deletes
            self._confirm = it
            btn.configure(text="Chắc chắn?", fg_color="#5B1F1F", text_color=TEXT, width=100)
            status.configure(text="Bấm 'Chắc chắn?' để xoá vĩnh viễn thư mục dự án này.", text_color=DANGER)
            return
        try:
            shutil.rmtree(it["dir"])
        except OSError as e:
            status.configure(text=f"Không xoá được: {e}", text_color=DANGER)
            return
        self._refresh_projects()

    def _clean_project(self, it, status):
        freed = 0
        for f in it["temp_files"]:
            try:
                freed += os.path.getsize(f)
                os.remove(f)
            except OSError:
                pass
        status.configure(text=f"Đã giải phóng {human(freed)}. Clip đã xuất giữ nguyên; dự án này không chỉnh sửa "
                              f"được nữa.", text_color=OK)
        self.after(1500, self._refresh_projects)

    def _rename_project(self, it, label, status):
        from .pipeline import safe_name
        dlg = ctk.CTkInputDialog(text="Tên mới cho dự án:", title="Đổi tên dự án")
        new = (dlg.get_input() or "").strip()
        if not new:
            return
        prefix = os.path.basename(it["dir"]).split(" - ", 1)[0] if " - " in os.path.basename(it["dir"]) else ""
        target = os.path.join(os.path.dirname(it["dir"]), (f"{prefix} - " if prefix else "") + safe_name(new))
        if os.path.exists(target):
            status.configure(text="Đã có dự án trùng tên trong ngày này.", text_color=DANGER)
            return
        try:
            os.rename(it["dir"], target)
            pj = os.path.join(target, "project.json")
            if os.path.exists(pj):
                proj = json.load(open(pj, encoding="utf-8"))
                proj["title"] = new
                json.dump(proj, open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except OSError as e:
            status.configure(text=f"Không đổi tên được (đang mở file trong thư mục?): {e}", text_color=DANGER)
            return
        self._refresh_projects()

    # ---------------- settings page ----------------
    def _build_settings(self):
        page = ctk.CTkScrollableFrame(self, fg_color=BG, corner_radius=0)
        page.grid_columnconfigure(0, weight=1)
        self.pages["settings"] = page
        ctk.CTkLabel(page, text="Cài đặt", font=self.font(28, "bold"), text_color=TEXT, anchor="w").grid(
            row=0, column=0, sticky="w", padx=36, pady=(30, 16))

        ai = self.card(page)
        ai.grid(row=1, column=0, sticky="ew", padx=36, pady=(0, 14))
        ai.grid_columnconfigure(1, weight=1)
        self.h(ai, "Tài khoản AI").grid(row=0, column=0, columnspan=4, sticky="w", padx=20, pady=(16, 2))
        self.muted(ai, "API key được mã hoá trên máy bạn và chỉ gửi tới nhà cung cấp tương ứng.", 12).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=20, pady=(0, 8))
        self.keys, self.models, self.key_status = {}, {}, {}
        r = 2
        self.base_url = None
        for p, name in PROVIDERS[1:]:
            if p == "custom":  # OpenAI-compatible gateway: needs an address first
                ctk.CTkFrame(ai, height=1, fg_color=BORDER).grid(row=r, column=0, columnspan=4, sticky="ew",
                                                                 padx=20, pady=(8, 8))
                r += 1
                ctk.CTkLabel(ai, text=name, font=self.font(14, "bold"), text_color=TEXT, anchor="w").grid(
                    row=r, column=0, sticky="w", padx=20, pady=6)
                self.base_url = ctk.CTkEntry(ai, height=36, corner_radius=10, border_width=0, fg_color=CARD2,
                                             font=self.font(13), text_color=TEXT,
                                             placeholder_text="Địa chỉ API, ví dụ https://api.vilao.ai/v1")
                self.base_url.insert(0, (self.cfg.get("base_urls") or {}).get("custom", ""))
                self.base_url.grid(row=r, column=1, sticky="ew", pady=6)
                self.base_url.bind("<FocusOut>", lambda _e: self._save_settings())
                presets = ctk.CTkFrame(ai, fg_color="transparent")
                presets.grid(row=r, column=2, columnspan=2, sticky="w", padx=8)
                for i, (label, url) in enumerate(llm.GATEWAY_PRESETS.items()):
                    self.button(presets, label, lambda u=url: self._set_base(u), width=90, height=32).grid(
                        row=0, column=i, padx=(0, 6))
                r += 1
                self.muted(ai, "Dùng cho cổng trung gian theo chuẩn OpenAI (vilao.ai, OpenRouter...): key dạng "
                               "'sk-...' không phải key Google/OpenAI.", 11).grid(row=r, column=1, columnspan=3,
                                                                                 sticky="w")
                r += 1
            ctk.CTkLabel(ai, text="" if p == "custom" else name, font=self.font(14, "bold"), text_color=TEXT,
                         width=90, anchor="w").grid(row=r, column=0, sticky="w", padx=20, pady=6)
            e = ctk.CTkEntry(ai, show="•", height=36, corner_radius=10, border_width=0, fg_color=CARD2,
                             font=self.font(13), text_color=TEXT, placeholder_text="API key")
            e.insert(0, config.get_key(self.cfg, p))
            e.grid(row=r, column=1, sticky="ew", pady=6)
            e.bind("<FocusOut>", lambda _e, p=p: self._save_settings())
            self.keys[p] = e
            models = llm.DEFAULT_MODELS.get(p) or []
            cur = self.cfg["models"].get(p) or (models[0] if models else "")
            m = ctk.CTkComboBox(ai, values=models or [cur], height=36, width=230, corner_radius=10, border_width=0,
                                fg_color=CARD2, button_color=BORDER, font=self.font(13), dropdown_font=self.font(13),
                                command=lambda _v: self._save_settings())
            m.set(cur)
            m.grid(row=r, column=2, padx=8, pady=6)
            self.models[p] = m
            self.button(ai, "Tải model", lambda p=p: self._load_models(p), width=96, height=36).grid(row=r, column=3,
                                                                                                     padx=(0, 20))
            r += 1
            if p in KEY_LINKS:
                link = ctk.CTkLabel(ai, text=f"Lấy API key {name} ↗", font=self.font(12), text_color=ACCENT,
                                    cursor="hand2", anchor="w")
                link.bind("<Button-1>", lambda _e, p=p: webbrowser.open(KEY_LINKS[p]))
                link.grid(row=r, column=1, sticky="w")
            st = self.muted(ai, "", 12)
            st.grid(row=r, column=2, columnspan=2, sticky="w", padx=8)
            self.key_status[p] = st
            r += 1
        self.key_status[p].grid_configure(pady=(0, 16))

        dl = self.card(page)
        dl.grid(row=2, column=0, sticky="ew", padx=36, pady=(0, 14))
        dl.grid_columnconfigure(0, weight=1)
        self.h(dl, "Tải video").grid(row=0, column=0, sticky="w", padx=20, pady=(16, 2))
        self.muted(dl, "Khi YouTube đòi đăng nhập ('not a bot', video giới hạn tuổi), dùng phiên đăng nhập YouTube trong "
                       "trình duyệt của bạn.\nFirefox ổn định nhất. Với Chrome/Edge hãy đóng trình duyệt trước khi tải.",
                   12).grid(row=1, column=0, sticky="w", padx=20)
        self.browser = self.seg(dl, BROWSERS, self.cfg.get("browser", ""), lambda _: self._save_settings())
        self.browser.grid(row=2, column=0, sticky="w", padx=20, pady=(10, 18))

        gen = self.card(page)
        gen.grid(row=3, column=0, sticky="ew", padx=36, pady=(0, 30))
        gen.grid_columnconfigure(1, weight=1)
        self.h(gen, "Chung").grid(row=0, column=0, columnspan=3, sticky="w", padx=20, pady=(16, 8))
        self.muted(gen, "Ngôn ngữ tiêu đề & caption", 13).grid(row=1, column=0, sticky="w", padx=20, pady=6)
        self.lang = self.seg(gen, LANGS, self.cfg.get("language", "auto"), lambda _: self._save_settings())
        self.lang.grid(row=1, column=1, sticky="w", pady=6)
        self.muted(gen, "Chép lời (Whisper) khi video\nkhông có phụ đề", 13).grid(row=2, column=0, sticky="w", padx=20,
                                                                                  pady=6)
        self.whisper = self.seg(gen, [("small", "Nhanh"), ("medium", "Cân bằng"), ("large-v3", "Chính xác nhất")],
                                self.cfg.get("whisper_model", "small"), lambda _: self._save_settings())
        self.whisper.grid(row=2, column=1, sticky="w", pady=6)
        self.muted(gen, "Xuất video", 13).grid(row=4, column=0, sticky="w", padx=20, pady=(6, 18))
        self.encoder = self.seg(gen, ENCODERS, self.cfg.get("encoder", "auto"), lambda _: self._save_settings())
        self.encoder.grid(row=4, column=1, sticky="w", pady=(6, 18))
        self.muted(gen, "Lưu clip vào", 13).grid(row=3, column=0, sticky="w", padx=20, pady=6)
        self.outdir = ctk.CTkEntry(gen, height=36, corner_radius=10, border_width=0, fg_color=CARD2, font=self.font(13),
                                   text_color=TEXT)
        self.outdir.insert(0, self.cfg["output_dir"])
        self.outdir.grid(row=3, column=1, sticky="ew", pady=6)
        self.button(gen, "Đổi...", self._pick_out, width=80, height=36).grid(row=3, column=2, padx=20, pady=6)

    def _set_base(self, url):
        self.base_url.delete(0, "end")
        self.base_url.insert(0, url)
        self._save_settings()
        self.key_status["custom"].configure(text="Đã điền địa chỉ. Dán key rồi bấm Tải model.", text_color=MUTED)

    def _save_settings(self):
        if self.base_url is not None:
            self.cfg.setdefault("base_urls", {})["custom"] = self.base_url.get().strip()
        for p, e in self.keys.items():
            config.set_key(self.cfg, p, e.get())
            self.cfg["models"][p] = self.models[p].get().strip()
        self.cfg["browser"] = key_of(BROWSERS, self.browser.get())
        self.cfg["encoder"] = key_of(ENCODERS, self.encoder.get())
        self.cfg["language"] = key_of(LANGS, self.lang.get())
        self.cfg["whisper_model"] = key_of([("small", "Nhanh"), ("medium", "Cân bằng"),
                                            ("large-v3", "Chính xác nhất")], self.whisper.get())
        self.cfg["output_dir"] = self.outdir.get().strip() or config.default_output_dir()
        config.save(self.cfg)
        self._provider_hint()

    def _load_models(self, p):
        key = self.keys[p].get().strip()
        self.key_status[p].configure(text="Đang tải danh sách model...", text_color=MUTED)

        def work():
            try:
                base = self.base_url.get().strip() if (p == "custom" and self.base_url is not None) else None
                self.q.put(("models", (p, llm.list_models(p, key, base), None)))
            except llm.AIError as e:
                self.q.put(("models", (p, None, str(e))))
            except Exception as e:
                self.q.put(("models", (p, None, f"Lỗi: {e}")))
        threading.Thread(target=work, daemon=True).start()

    # ---------------- actions ----------------
    def _pick_file(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(title="Chọn video", filetypes=[
            ("Video", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"), ("Tất cả", "*.*")])
        if p:
            self.src.delete(0, "end")
            self.src.insert(0, p)

    def _pick_out(self):
        from tkinter import filedialog
        p = filedialog.askdirectory(title="Chọn thư mục lưu clip")
        if p:
            self.outdir.delete(0, "end")
            self.outdir.insert(0, p)
            self._save_settings()

    def _collect_cfg(self):
        self._save_settings()
        c = self.cfg
        c["provider"] = key_of(PROVIDERS, self.provider.get())
        c["num_clips"] = self._num_value()
        c["length"] = key_of(LENGTHS, self.length.get())
        c["framing"] = key_of(FRAMINGS, self.framing.get())
        c["zoom_fx"] = key_of(ZOOMFX, self.zoomfx.get())
        c["topic"] = self.topic.get().strip()
        c["highlight"] = self.color
        c["captions"], c["uppercase"], c["hook"] = bool(self.cap.get()), bool(self.upper.get()), bool(self.hook.get())
        config.save(c)
        return c

    def _start(self):
        if self.job:
            return
        src = self.src.get().strip().strip('"')
        if not src:
            self._flash("Dán link hoặc chọn file video trước.")
            return
        if not is_url(src) and not os.path.isfile(src):
            self._flash("Không tìm thấy file. Kiểm tra lại đường dẫn hoặc dán link.")
            return
        try:
            self._num_value()
        except ValueError as e:
            self._flash(str(e))
            return
        cfg = self._collect_cfg()
        if cfg["provider"] != "offline" and not config.get_key(cfg, cfg["provider"]):
            self._flash(f"Chưa có API key {label_of(PROVIDERS, cfg['provider'])}. Vào Cài đặt để thêm, hoặc chọn Offline.")
            return
        self.res.grid_remove()
        self.logbox.delete("1.0", "end")
        self.prog.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        self.bar.set(0)
        self.pct.configure(text="0%")
        self.status.configure(text="Đang bắt đầu...", text_color=TEXT)
        self._set_step(0)
        self.go.configure(state="disabled", text="Đang xử lý...")
        self.stop.configure(state="normal")
        self.job = Job(src, cfg, log=lambda m: self.q.put(("log", m)),
                       progress=lambda f, t: self.q.put(("progress", (f, t))),
                       done=lambda r, e: self.q.put(("done", (r, e))))
        self.job.start()

    def _flash(self, text):
        self.hint.configure(text=text, text_color=DANGER)

    def _cancel(self):
        if self.job:
            self.job.cancel()
            self.status.configure(text="Đang huỷ...")

    # ---------------- queue pump ----------------
    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.logbox.insert("end", data + "\n")
                    self.logbox.see("end")
                elif kind == "progress":
                    f, t = data
                    self.bar.set(f)
                    self.pct.configure(text=f"{int(f * 100)}%")
                    self.status.configure(text=t)
                    self._set_step(f)
                elif kind == "models":
                    p, models, err = data
                    if err:
                        self.key_status[p].configure(text=err, text_color=DANGER)
                    elif models:
                        cur = self.models[p].get()
                        self.models[p].configure(values=models)
                        self.models[p].set(cur if cur in models else models[0])
                        self.key_status[p].configure(text=f"✓ {len(models)} model khả dụng", text_color=OK)
                        self._save_settings()
                    else:
                        self.key_status[p].configure(text="Tài khoản chưa có model phù hợp.", text_color=DANGER)
                elif kind == "done":
                    res, err = data
                    self.job = None
                    self.go.configure(state="normal", text="Tạo clip  →")
                    self.stop.configure(state="disabled")
                    if err:
                        self.status.configure(text=err, text_color=DANGER)
                        self._set_step(self.bar.get(), failed=True)
                        self.logbox.insert("end", "\n" + err + "\n")
                    else:
                        self.bar.set(1)
                        self.pct.configure(text="100%")
                        self._set_step(1.0)
                        msg = f"Xong! Đã tạo {len(res['clips'])} clip."
                        if res.get("note"):
                            msg += "\n" + res["note"]
                        self.status.configure(text=msg, text_color=OK if not res.get("note") else "#FBBF24")
                        if self.log_visible:
                            self._toggle_log()
                        self._show_results(res)
                        self.after(150, lambda: self.pages["create"]._parent_canvas.yview_moveto(1.0))
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _on_close(self):
        try:
            self._collect_cfg()
        except Exception:
            pass
        if self.job:
            self.job.cancel()
        self.destroy()


def main():
    App().mainloop()
