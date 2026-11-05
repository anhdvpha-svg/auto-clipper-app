"""Auto Clipper desktop app - paste a link or pick a file, get vertical clips."""
import os, queue, subprocess, sys, threading, webbrowser

import customtkinter as ctk

from . import config, llm
from .pipeline import Job, is_url

APP_TITLE = "Auto Clipper"
PROVIDER_LABELS = {"offline": "Offline (không cần AI)", "claude": "Claude", "openai": "ChatGPT", "gemini": "Gemini"}
KEY_LINKS = {"claude": "https://console.anthropic.com/settings/keys",
             "openai": "https://platform.openai.com/api-keys",
             "gemini": "https://aistudio.google.com/apikey"}
LENGTH_LABELS = {"15-30": "15-30 giây", "30-60": "30-60 giây", "60-90": "60-90 giây"}
LANG_LABELS = {"auto": "Theo video", "vi": "Tiếng Việt", "en": "English"}
FRAMING_LABELS = {"auto": "Tự động theo từng cảnh", "center": "Luôn cắt giữa", "blur": "Giữ khung + nền mờ"}
COLORS = {"Vàng": "FFE600", "Xanh ngọc": "00E5FF", "Hồng": "FF4FA3", "Xanh lá": "5BFF6B", "Trắng": "FFFFFF"}
NUM_LABELS = ["Tự động"] + [str(i) for i in range(1, 11)]

ACCENT = "#E8C500"


def open_path(p):
    if os.name == "nt":
        os.startfile(p)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def _inv(d, label):
    return next(k for k, v in d.items() if v == label)


class Section(ctk.CTkFrame):
    def __init__(self, master, title):
        super().__init__(master, corner_radius=10)
        self.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 4))
        self.row = 1

    def add(self, label, widget, extra=None):
        if label:
            ctk.CTkLabel(self, text=label).grid(row=self.row, column=0, sticky="w", padx=(14, 8), pady=5)
            col, span, left = 1, 1 if extra else 2, 0
        else:  # no label: use the full width
            col, span, left = 0, 2 if extra else 3, 14
        widget.grid(row=self.row, column=col, columnspan=span, sticky="ew",
                    padx=(left, 14 if not extra else 6), pady=5)
        if extra:
            extra.grid(row=self.row, column=2, sticky="e", padx=(0, 14), pady=5)
        self.row += 1
        return widget


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.cfg = config.load()
        self.title(APP_TITLE)
        self.geometry("1180x780")
        self.minsize(980, 640)
        try:
            ico = config.resource_path("assets", "icon.ico")
            if os.name == "nt" and os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass
        self.q = queue.Queue()
        self.job = None
        self.grid_columnconfigure(0, weight=0, minsize=470)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_left()
        self._build_right()
        self._load_provider()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- layout ----------------
    def _build_left(self):
        left = ctk.CTkScrollableFrame(self, width=450, corner_radius=0, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)
        left.grid_columnconfigure(0, weight=1)

        s = Section(left, "1. Video")
        s.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.src = ctk.CTkEntry(s, placeholder_text="Dán link YouTube / Facebook / TikTok, hoặc chọn file")
        s.add(None, self.src, ctk.CTkButton(s, text="Chọn file...", width=90, command=self._pick_file))

        s = Section(left, "2. AI chọn đoạn hay")
        s.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.provider = s.add("Dùng", ctk.CTkOptionMenu(s, values=list(PROVIDER_LABELS.values()),
                                                         command=lambda _: self._on_provider()))
        self.key = ctk.CTkEntry(s, show="•", placeholder_text="API key")
        self.key_toggle = ctk.CTkButton(s, text="Hiện", width=60, command=self._toggle_key)
        s.add("API key", self.key, self.key_toggle)
        self.model = ctk.CTkComboBox(s, values=[""])
        s.add("Model", self.model, ctk.CTkButton(s, text="Tải danh sách", width=100, command=self._load_models))
        self.key_link = ctk.CTkLabel(s, text="", text_color=ACCENT, cursor="hand2", anchor="w")
        self.key_link.bind("<Button-1>", lambda _: webbrowser.open(KEY_LINKS.get(self._provider(), "")))
        s.add(None, self.key_link)
        self.ai_note = ctk.CTkLabel(s, text="", wraplength=410, justify="left", anchor="w", text_color="gray70")
        s.add(None, self.ai_note)

        s = Section(left, "3. Clip")
        s.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.num = s.add("Số clip", ctk.CTkOptionMenu(s, values=NUM_LABELS))
        self.num.set(NUM_LABELS[self.cfg["num_clips"]] if 0 <= self.cfg["num_clips"] <= 10 else "Tự động")
        self.length = s.add("Độ dài", ctk.CTkOptionMenu(s, values=list(LENGTH_LABELS.values())))
        self.length.set(LENGTH_LABELS.get(self.cfg["length"], "30-60 giây"))
        self.topic = s.add("Chỉ lấy", ctk.CTkEntry(s, placeholder_text="VD: đoạn hài hước, nói về marketing (để trống = hay nhất)"))
        if self.cfg.get("topic"):
            self.topic.insert(0, self.cfg["topic"])
        self.lang = s.add("Ngôn ngữ chữ", ctk.CTkOptionMenu(s, values=list(LANG_LABELS.values())))
        self.lang.set(LANG_LABELS.get(self.cfg["language"], "Theo video"))

        s = Section(left, "4. Hình & phụ đề")
        s.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.framing = s.add("Khung dọc", ctk.CTkOptionMenu(s, values=list(FRAMING_LABELS.values())))
        self.framing.set(FRAMING_LABELS.get(self.cfg["framing"], FRAMING_LABELS["auto"]))
        self.color = s.add("Màu chữ nhấn", ctk.CTkOptionMenu(s, values=list(COLORS)))
        self.color.set(next((k for k, v in COLORS.items() if v == self.cfg.get("highlight")), "Vàng"))
        row = ctk.CTkFrame(s, fg_color="transparent")
        self.cap = ctk.CTkCheckBox(row, text="Phụ đề")
        self.upper = ctk.CTkCheckBox(row, text="IN HOA")
        self.hook = ctk.CTkCheckBox(row, text="Tiêu đề hook")
        for i, (w, k) in enumerate(((self.cap, "captions"), (self.upper, "uppercase"), (self.hook, "hook"))):
            w.grid(row=0, column=i, padx=(0, 14))
            (w.select if self.cfg.get(k, True) else w.deselect)()
        s.add(None, row)

        s = Section(left, "5. Nâng cao")
        s.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        self.whisper = s.add("Chép lời (Whisper)", ctk.CTkOptionMenu(s, values=["small", "medium", "large-v3"]))
        self.whisper.set(self.cfg.get("whisper_model", "small"))
        self.outdir = ctk.CTkEntry(s)
        self.outdir.insert(0, self.cfg["output_dir"])
        s.add("Lưu vào", self.outdir, ctk.CTkButton(s, text="Đổi...", width=60, command=self._pick_out))

    def _build_right(self):
        right = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(3, weight=1)

        top = ctk.CTkFrame(right, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        self.go = ctk.CTkButton(top, text="▶  Tạo clip", height=46, font=ctk.CTkFont(size=16, weight="bold"),
                                fg_color=ACCENT, text_color="black", hover_color="#c9a900", command=self._start)
        self.go.grid(row=0, column=0, sticky="ew")
        self.stop = ctk.CTkButton(top, text="Huỷ", width=80, height=46, state="disabled", command=self._cancel,
                                  fg_color="gray30")
        self.stop.grid(row=0, column=1, padx=(8, 0))

        self.bar = ctk.CTkProgressBar(right, progress_color=ACCENT)
        self.bar.set(0)
        self.bar.grid(row=1, column=0, sticky="ew", pady=(12, 4))
        self.status = ctk.CTkLabel(right, text="Sẵn sàng. Dán link hoặc chọn file video rồi bấm Tạo clip.", anchor="w")
        self.status.grid(row=2, column=0, sticky="ew")

        tabs = ctk.CTkTabview(right)
        tabs.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        self.tab_res = tabs.add("Kết quả")
        self.tab_log = tabs.add("Nhật ký")
        self.tab_res.grid_columnconfigure(0, weight=1)
        self.tab_res.grid_rowconfigure(0, weight=1)
        self.results = ctk.CTkScrollableFrame(self.tab_res, fg_color="transparent")
        self.results.grid(row=0, column=0, sticky="nsew")
        self.results.grid_columnconfigure(0, weight=1)
        self.open_out = ctk.CTkButton(self.tab_res, text="Mở thư mục clip", state="disabled", command=self._open_out)
        self.open_out.grid(row=1, column=0, sticky="e", pady=(6, 0))
        self.tab_log.grid_columnconfigure(0, weight=1)
        self.tab_log.grid_rowconfigure(0, weight=1)
        self.logbox = ctk.CTkTextbox(self.tab_log, font=ctk.CTkFont(family="Consolas", size=12))
        self.logbox.grid(row=0, column=0, sticky="nsew")
        self.tabs = tabs
        self._empty_results("Clip sẽ hiện ở đây sau khi xử lý xong.")

    # ---------------- provider / key / model ----------------
    def _provider(self):
        return _inv(PROVIDER_LABELS, self.provider.get())

    def _load_provider(self):
        self.provider.set(PROVIDER_LABELS.get(self.cfg.get("provider"), PROVIDER_LABELS["offline"]))
        self._prev_provider = self._provider()
        self._on_provider(save_prev=False)

    def _on_provider(self, save_prev=True):
        if save_prev:
            self._stash_provider_fields(self._prev_provider)
        p = self._provider()
        self._prev_provider = p
        ai = p != "offline"
        for w in (self.key, self.model, self.key_toggle):
            w.configure(state="normal" if ai else "disabled")
        self.key.delete(0, "end")
        if ai:
            self.key.insert(0, config.get_key(self.cfg, p))
            models = llm.DEFAULT_MODELS.get(p) or []
            cur = self.cfg["models"].get(p) or (models[0] if models else "")
            self.model.configure(values=models or [cur or ""])
            self.model.set(cur)
            self.key_link.configure(text=f"Lấy API key {PROVIDER_LABELS[p]} ↗")
            self.ai_note.configure(text="AI chọn đoạn, chấm điểm viral, viết hook và caption. Bạn trả phí cho "
                                        "nhà cung cấp theo lượt dùng (thường vài trăm đến vài nghìn đồng mỗi video).")
        else:
            self.model.set("")
            self.key_link.configure(text="")
            self.ai_note.configure(text="Miễn phí, không cần mạng cho bước chọn đoạn. Chọn theo âm lượng, chuyển động "
                                        "và câu mở đầu; kém tinh tế hơn AI.")

    def _stash_provider_fields(self, p):
        if p and p != "offline":
            config.set_key(self.cfg, p, self.key.get())
            self.cfg["models"][p] = self.model.get().strip()

    def _toggle_key(self):
        hidden = self.key.cget("show") == "•"
        self.key.configure(show="" if hidden else "•")
        self.key_toggle.configure(text="Ẩn" if hidden else "Hiện")

    def _load_models(self):
        p = self._provider()
        if p == "offline":
            return
        key = self.key.get().strip()
        self.status.configure(text="Đang tải danh sách model...")

        def work():
            try:
                models = llm.list_models(p, key)
                self.q.put(("models", (p, models)))
            except llm.AIError as e:
                self.q.put(("status", str(e)))
            except Exception as e:
                self.q.put(("status", f"Không tải được danh sách model: {e}"))
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

    def _collect_cfg(self):
        self._stash_provider_fields(self._provider())
        c = self.cfg
        c["provider"] = self._provider()
        n = self.num.get()
        c["num_clips"] = 0 if n == "Tự động" else int(n)
        c["length"] = _inv(LENGTH_LABELS, self.length.get())
        c["topic"] = self.topic.get().strip()
        c["language"] = _inv(LANG_LABELS, self.lang.get())
        c["framing"] = _inv(FRAMING_LABELS, self.framing.get())
        c["highlight"] = COLORS[self.color.get()]
        c["captions"], c["uppercase"], c["hook"] = bool(self.cap.get()), bool(self.upper.get()), bool(self.hook.get())
        c["whisper_model"] = self.whisper.get()
        c["output_dir"] = self.outdir.get().strip() or config.default_output_dir()
        config.save(c)
        return c

    def _start(self):
        src = self.src.get().strip()
        if not src:
            self.status.configure(text="Dán link hoặc chọn file video trước.")
            return
        if not is_url(src) and not os.path.isfile(src.strip('"')):
            self.status.configure(text="Không tìm thấy file. Kiểm tra lại đường dẫn hoặc dán link.")
            return
        cfg = self._collect_cfg()
        if cfg["provider"] != "offline" and not config.get_key(cfg, cfg["provider"]):
            self.status.configure(text=f"Nhập API key cho {PROVIDER_LABELS[cfg['provider']]} hoặc chọn Offline.")
            return
        self.logbox.delete("1.0", "end")
        self._empty_results("Đang xử lý...")
        self.go.configure(state="disabled")
        self.stop.configure(state="normal")
        self.open_out.configure(state="disabled")
        self.tabs.set("Nhật ký")
        self.job = Job(src, cfg,
                       log=lambda m: self.q.put(("log", m)),
                       progress=lambda f, t: self.q.put(("progress", (f, t))),
                       done=lambda r, e: self.q.put(("done", (r, e))))
        self.job.start()

    def _cancel(self):
        if self.job:
            self.job.cancel()
            self.status.configure(text="Đang huỷ sau bước hiện tại...")

    def _open_out(self):
        if getattr(self, "last_out", None):
            open_path(self.last_out)

    # ---------------- results ----------------
    def _empty_results(self, text):
        for w in self.results.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.results, text=text, text_color="gray60").grid(row=0, column=0, pady=30)

    def _show_results(self, res):
        from PIL import Image
        for w in self.results.winfo_children():
            w.destroy()
        for i, c in enumerate(sorted(res["clips"], key=lambda c: -c["score"]["total"])):
            card = ctk.CTkFrame(self.results, corner_radius=10)
            card.grid(row=i, column=0, sticky="ew", pady=5)
            card.grid_columnconfigure(1, weight=1)
            try:
                im = Image.open(c["thumb"])
                im.thumbnail((90, 160))
                ci = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
                ctk.CTkLabel(card, image=ci, text="").grid(row=0, column=0, rowspan=3, padx=10, pady=10)
            except Exception:
                pass
            ctk.CTkLabel(card, text=f"{c['score']['total']}", font=ctk.CTkFont(size=22, weight="bold"),
                         text_color=ACCENT).grid(row=0, column=2, rowspan=2, padx=14)
            ctk.CTkLabel(card, text=c["title"], font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
                         wraplength=420, justify="left").grid(row=0, column=1, sticky="ew", pady=(10, 0))
            ctk.CTkLabel(card, text=f"{c['length']:.0f} giây · {c.get('reason', '')}", anchor="w", wraplength=420,
                         justify="left", text_color="gray70").grid(row=1, column=1, sticky="ew")
            btns = ctk.CTkFrame(card, fg_color="transparent")
            btns.grid(row=2, column=1, sticky="w", pady=(4, 10))
            ctk.CTkButton(btns, text="Xem", width=70, command=lambda p=c["file"]: open_path(p)).grid(row=0, column=0)
            cap = c["captions"].get("TikTok") or ""
            tags = " ".join(c.get("hashtags") or [])
            ctk.CTkButton(btns, text="Copy caption", width=110, fg_color="gray30",
                          command=lambda t=f"{cap}\n{tags}".strip(): self._copy(t)).grid(row=0, column=1, padx=6)
        self.last_out = res["out_dir"]
        self.open_out.configure(state="normal")

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.configure(text="Đã copy caption.")

    # ---------------- queue pump ----------------
    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.logbox.insert("end", data + "\n")
                    self.logbox.see("end")
                elif kind == "progress":
                    self.bar.set(data[0])
                    self.status.configure(text=data[1])
                elif kind == "status":
                    self.status.configure(text=data)
                elif kind == "models":
                    p, models = data
                    if p == self._provider() and models:
                        cur = self.model.get()
                        self.model.configure(values=models)
                        self.model.set(cur if cur in models else models[0])
                        self.status.configure(text=f"Đã tải {len(models)} model. Chọn model rồi bấm Tạo clip.")
                    elif not models:
                        self.status.configure(text="Tài khoản không có model phù hợp.")
                elif kind == "done":
                    res, err = data
                    self.go.configure(state="normal")
                    self.stop.configure(state="disabled")
                    self.job = None
                    if err:
                        self.bar.set(0)
                        self.status.configure(text=err)
                        self.logbox.insert("end", "\n" + err + "\n")
                        self._empty_results(err)
                    else:
                        self._show_results(res)
                        self.tabs.set("Kết quả")
                        self.status.configure(text=f"Xong! {len(res['clips'])} clip. Bấm 'Xem' để mở, hoặc mở thư mục.")
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
