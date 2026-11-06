"""Manual editing of one rendered clip: hook title, captions, timing, post caption -> re-render that clip."""
import contextlib, io, json, os, threading

import customtkinter as ctk

from .engine import render_clips


class ProjectError(Exception):
    pass


def load_project(out_dir):
    pj = os.path.join(out_dir, "project.json")
    if not os.path.exists(pj):
        raise ProjectError("Dự án này được tạo bằng bản cũ nên không chỉnh sửa được. Hãy tạo lại clip.")
    proj = json.load(open(pj, encoding="utf-8"))
    data = json.load(open(os.path.join(out_dir, "clips_rendered.json"), encoding="utf-8"))
    return proj, data


def transcript_words(proj):
    t = proj.get("transcript")
    if t and os.path.exists(t):
        return render_clips.all_words(json.load(open(t, encoding="utf-8")))
    return []


def default_chunks(rendered, words):
    cw, _ = render_clips.clip_words_of([tuple(r) for r in rendered["ranges"]], words)
    return render_clips.caption_chunks(cw)


def rerender(out_dir, index, clip, log=print):
    """Save the edited clip spec and re-render only that clip. Returns the new rendered entry."""
    proj, data = load_project(out_dir)
    data["clips"][index] = clip
    old = data["rendered"][index]
    clips_path = os.path.join(proj["work"] if os.path.isdir(proj.get("work", "")) else out_dir, "clips_edit.json")
    json.dump({"clips": data["clips"]}, open(clips_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    argv = list(proj["base_args"]) + ["--clips", clips_path, "--only", str(index + 1)]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            render_clips.main(argv)
    except SystemExit as e:
        if e.code not in (None, 0):
            raise ProjectError(str(e.code))
    finally:
        log(buf.getvalue())
    new = json.load(open(os.path.join(out_dir, "clips_rendered.json"), encoding="utf-8"))["rendered"][index]
    if old["file"] != new["file"]:  # title changed -> new file name; remove the stale one
        for f in (old["file"], old["thumb"]):
            try:
                os.remove(os.path.join(out_dir, f))
            except OSError:
                pass
    for f in os.listdir(out_dir):  # render scratch
        if f.endswith((".ass", "_hook.png")):
            try:
                os.remove(os.path.join(out_dir, f))
            except OSError:
                pass
    return new


POSITIONS = [("top", "Trên"), ("middle", "Giữa"), ("bottom", "Dưới")]
THEMES = [("light", "Nền trắng"), ("dark", "Nền đen"), ("accent", "Màu nhấn")]
COLORS = ["FFE600", "22D3EE", "FF4FA3", "5BFF6B", "FFFFFF"]


def _fmt(t):
    m, s = divmod(max(0.0, float(t)), 60)
    return f"{int(m):02d}:{s:04.1f}"


class ClipEditor(ctk.CTkToplevel):
    """`app` is the main window (fonts, colours, helpers); `on_saved(index, rendered)` refreshes the grid."""

    def __init__(self, app, out_dir, index, on_saved):
        super().__init__(app)
        self.app, self.out_dir, self.index, self.on_saved = app, out_dir, index, on_saved
        self.proj, data = load_project(out_dir)
        self.clip = json.loads(json.dumps(data["clips"][index]))
        self.rendered = data["rendered"][index]
        self.words = transcript_words(self.proj)
        T = app.theme
        self.T = T
        self.title(f"Chỉnh sửa · {self.clip.get('title', 'Clip')}")
        self.geometry("1040x760")
        self.minsize(940, 680)
        self.configure(fg_color=T["BG"])
        self.transient(app)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._img = None
        self._build_preview()
        self._build_tabs()
        self.after(50, self.lift)

    # ---------- layout ----------
    def _build_preview(self):
        T, f = self.T, self.app.font
        left = ctk.CTkFrame(self, fg_color=T["CARD"], corner_radius=16, border_width=1, border_color=T["BORDER"])
        left.grid(row=0, column=0, sticky="ns", padx=(18, 9), pady=18)
        self.preview = ctk.CTkLabel(left, text="")
        self.preview.grid(row=0, column=0, padx=16, pady=(16, 8))
        self._show_thumb()
        self.app.button(left, "▶  Xem clip", lambda: self.app.open_path(os.path.join(self.out_dir, self.rendered["file"])),
                        height=36).grid(row=1, column=0, sticky="ew", padx=16)
        self.save_btn = self.app.button(left, "Dựng lại clip", self._save, primary=True, height=44)
        self.save_btn.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 4))
        self.msg = ctk.CTkLabel(left, text="Sửa xong bấm Dựng lại clip.", font=f(12), text_color=T["MUTED"],
                                  wraplength=250, justify="left")
        self.msg.grid(row=3, column=0, sticky="w", padx=16, pady=(4, 16))

    def _show_thumb(self):
        from PIL import Image, ImageOps
        try:
            im = ImageOps.fit(Image.open(os.path.join(self.out_dir, self.rendered["thumb"])).convert("RGB"), (540, 960))
            self._img = ctk.CTkImage(im, im, size=(270, 480))
            self.preview.configure(image=self._img)
        except Exception:
            self.preview.configure(text="(chưa có ảnh xem trước)", width=270, height=480)

    def _build_tabs(self):
        T = self.T
        tabs = ctk.CTkTabview(self, fg_color=T["CARD"], segmented_button_selected_color=T["ACCENT"],
                              segmented_button_selected_hover_color=T["ACCENT_HOVER"],
                              segmented_button_unselected_color=T["CARD2"], text_color=T["TEXT"],
                              corner_radius=16, border_width=1, border_color=T["BORDER"])
        tabs.grid(row=0, column=1, sticky="nsew", padx=(9, 18), pady=18)
        self._hook_tab(tabs.add("Tiêu đề hook"))
        self._caption_tab(tabs.add("Phụ đề"))
        self._time_tab(tabs.add("Thời lượng"))
        self._post_tab(tabs.add("Caption đăng bài"))

    def _row(self, master, r, label, widget, hint=None):
        f = self.app.font
        ctk.CTkLabel(master, text=label, font=f(13), text_color=self.T["MUTED"], anchor="w").grid(
            row=r, column=0, sticky="nw", padx=(16, 12), pady=8)
        widget.grid(row=r, column=1, sticky="ew", padx=(0, 16), pady=8)
        if hint:
            ctk.CTkLabel(master, text=hint, font=f(11), text_color=self.T["MUTED"], anchor="w", justify="left").grid(
                row=r + 1, column=1, sticky="w", padx=(0, 16))

    def _slider(self, master, frm, to, value, steps, fmt):
        box = ctk.CTkFrame(master, fg_color="transparent")
        box.grid_columnconfigure(0, weight=1)
        lbl = ctk.CTkLabel(box, text=fmt(value), font=self.app.font(13), text_color=self.T["TEXT"], width=60)
        s = ctk.CTkSlider(box, from_=frm, to=to, number_of_steps=steps, progress_color=self.T["ACCENT"],
                          button_color=self.T["TEXT"], button_hover_color=self.T["TEXT"],
                          command=lambda v: lbl.configure(text=fmt(v)))
        s.set(value)
        s.grid(row=0, column=0, sticky="ew")
        lbl.grid(row=0, column=1, padx=(10, 0))
        return box, s

    # ---------- hook ----------
    def _hook_tab(self, t):
        T, f, app = self.T, self.app.font, self.app
        t.grid_columnconfigure(1, weight=1)
        hs = self.clip.get("hook_style") or {}
        self.hook_on = ctk.CTkSwitch(t, text="Hiện tiêu đề hook", font=f(13), text_color=T["TEXT"],
                                     progress_color=T["ACCENT"])
        (self.hook_on.select if hs.get("enabled", True) else self.hook_on.deselect)()
        self.hook_on.grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(14, 6))
        self.hook_text = ctk.CTkTextbox(t, height=96, fg_color=T["CARD2"], text_color=T["TEXT"], font=f(16, "bold"),
                                        corner_radius=10, wrap="word")
        self.hook_text.insert("1.0", self.clip.get("hook_title", ""))
        self._row(t, 1, "Nội dung", self.hook_text, "Enter để tự xuống dòng. Nên ≤ 8 chữ: cụ thể, gây tò mò, "
                                                    "đúng nội dung clip.")
        self.hook_pos = app.seg(t, POSITIONS, hs.get("position", "top"))
        self._row(t, 3, "Vị trí", self.hook_pos)
        self.hook_theme = app.seg(t, THEMES, hs.get("theme", "light"))
        self._row(t, 4, "Kiểu khung", self.hook_theme)
        box, self.hook_size = self._slider(t, 0.6, 1.6, float(hs.get("size", 1.0)), 10, lambda v: f"{v * 100:.0f}%")
        self._row(t, 5, "Cỡ chữ", box)
        length = float(self.rendered["length"])
        secs = hs.get("seconds", self.clip.get("hook_seconds", 4.0))
        allc = secs in ("all", None, 0)
        box, self.hook_secs = self._slider(t, 1, max(2, min(30, length)), 4.0 if allc else float(secs),
                                           int(max(2, min(30, length))) - 1, lambda v: f"{v:.0f} giây")
        self._row(t, 6, "Hiện trong", box)
        self.hook_all = ctk.CTkSwitch(t, text="Hiện suốt cả clip", font=f(13), text_color=T["TEXT"],
                                      progress_color=T["ACCENT"])
        (self.hook_all.select if allc else self.hook_all.deselect)()
        self.hook_all.grid(row=7, column=1, sticky="w", padx=(0, 16), pady=(0, 8))

    # ---------- captions ----------
    def _caption_tab(self, t):
        T, f, app = self.T, self.app.font, self.app
        t.grid_columnconfigure(1, weight=1)
        t.grid_rowconfigure(6, weight=1)
        cs = self.clip.get("caption_style") or {}
        top = ctk.CTkFrame(t, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(14, 4))
        self.cap_on = ctk.CTkSwitch(top, text="Hiện phụ đề", font=f(13), text_color=T["TEXT"], progress_color=T["ACCENT"])
        (self.cap_on.select if cs.get("enabled", True) else self.cap_on.deselect)()
        self.cap_on.grid(row=0, column=0, padx=(0, 18))
        self.cap_upper = ctk.CTkSwitch(top, text="IN HOA", font=f(13), text_color=T["TEXT"], progress_color=T["ACCENT"])
        (self.cap_upper.select if cs.get("uppercase", app.cfg.get("uppercase", True)) else self.cap_upper.deselect)()
        self.cap_upper.grid(row=0, column=1, padx=(0, 18))
        self.cap_color = cs.get("highlight", app.cfg.get("highlight", "FFE600"))
        self.cap_sw = {}
        for i, c in enumerate(COLORS):
            b = ctk.CTkButton(top, text="", width=26, height=26, corner_radius=13, fg_color=f"#{c}", hover_color=f"#{c}",
                              border_width=3, command=lambda c=c: self._cap_pick(c))
            b.grid(row=0, column=2 + i, padx=(0, 6))
            self.cap_sw[c] = b
        self._cap_pick(self.cap_color)
        box, self.cap_size = self._slider(t, 0.6, 1.6, float(cs.get("size", 1.0)), 10, lambda v: f"{v * 100:.0f}%")
        self._row(t, 1, "Cỡ chữ", box)
        box, self.cap_y = self._slider(t, 0.40, 0.92, float(cs.get("y", 0.75)), 26,
                                       lambda v: f"{v * 100:.0f}% từ trên")
        self._row(t, 2, "Vị trí dọc", box)
        ctk.CTkLabel(t, text="Sửa chữ từng dòng phụ đề (để trống một dòng để ẩn dòng đó):", font=f(13),
                     text_color=T["MUTED"], anchor="w").grid(row=4, column=0, columnspan=2, sticky="w", padx=16,
                                                             pady=(10, 4))
        self.reset_caps = ctk.CTkButton(t, text="Khôi phục phụ đề gốc", height=26, width=150, fg_color="transparent",
                                        hover_color=T["CARD2"], text_color=T["ACCENT"], font=f(12),
                                        command=self._reset_caps)
        self.reset_caps.grid(row=4, column=1, sticky="e", padx=16)
        self.cap_list = ctk.CTkScrollableFrame(t, fg_color=T["CARD2"], corner_radius=10)
        self.cap_list.grid(row=6, column=0, columnspan=2, sticky="nsew", padx=16, pady=(0, 14))
        self.cap_list.grid_columnconfigure(1, weight=1)
        self._fill_caps(self.clip.get("captions_override") or default_chunks(self.rendered, self.words))

    def _cap_pick(self, c):
        self.cap_color = c
        for k, b in self.cap_sw.items():
            b.configure(border_color=self.T["TEXT"] if k == c else self.T["CARD"])

    def _fill_caps(self, chunks):
        for w in self.cap_list.winfo_children():
            w.destroy()
        self.cap_rows = []
        if not chunks:
            ctk.CTkLabel(self.cap_list, text="Clip này không có lời nói nên không có phụ đề.", font=self.app.font(13),
                         text_color=self.T["MUTED"]).grid(row=0, column=0, columnspan=2, pady=20)
            return
        for i, c in enumerate(chunks):
            ctk.CTkLabel(self.cap_list, text=_fmt(c["s"]), font=self.app.font(12), text_color=self.T["MUTED"],
                         width=56).grid(row=i, column=0, padx=(6, 8), pady=3)
            e = ctk.CTkEntry(self.cap_list, height=32, corner_radius=8, border_width=0, fg_color=self.T["CARD"],
                             text_color=self.T["TEXT"], font=self.app.font(14))
            e.insert(0, c["text"])
            e.grid(row=i, column=1, sticky="ew", padx=(0, 6), pady=3)
            self.cap_rows.append((c, e))

    def _reset_caps(self):
        self.clip.pop("captions_override", None)
        self._fill_caps(default_chunks(self.rendered, self.words))

    # ---------- timing ----------
    def _time_tab(self, t):
        T, f = self.T, self.app.font
        t.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(t, text="Thời điểm bắt đầu và kết thúc trong video gốc (giây). Đổi thời lượng sẽ khôi phục "
                             "phụ đề về bản gốc vì mốc thời gian thay đổi.", font=f(12), text_color=T["MUTED"],
                     wraplength=560, justify="left", anchor="w").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                      padx=16, pady=(14, 8))
        self.t_start = self._time_row(t, 1, "Bắt đầu", float(self.clip.get("start", self.rendered["ranges"][0][0])))
        self.t_end = self._time_row(t, 2, "Kết thúc", float(self.clip.get("end", self.rendered["ranges"][-1][1])))
        self.t_len = ctk.CTkLabel(t, text="", font=f(13), text_color=T["TEXT"], anchor="w")
        self.t_len.grid(row=3, column=1, sticky="w", pady=8)
        self._upd_len()

    def _time_row(self, t, r, label, value):
        box = ctk.CTkFrame(t, fg_color="transparent")
        e = ctk.CTkEntry(box, width=110, height=34, corner_radius=8, border_width=0, fg_color=self.T["CARD2"],
                         text_color=self.T["TEXT"], font=self.app.font(14))
        e.insert(0, f"{value:.1f}")
        e.bind("<KeyRelease>", lambda _: self._upd_len())

        def nudge(d):
            try:
                v = max(0.0, float(e.get()) + d)
            except ValueError:
                return
            e.delete(0, "end")
            e.insert(0, f"{v:.1f}")
            self._upd_len()
        for i, (txt, d) in enumerate((("−1s", -1), ("−0.5s", -0.5))):
            self.app.button(box, txt, lambda d=d: nudge(d), width=58, height=34).grid(row=0, column=i, padx=(0, 6))
        e.grid(row=0, column=2, padx=(0, 6))
        for i, (txt, d) in enumerate((("+0.5s", 0.5), ("+1s", 1))):
            self.app.button(box, txt, lambda d=d: nudge(d), width=58, height=34).grid(row=0, column=3 + i, padx=(0, 6))
        self._row(t, r, label, box)
        return e

    def _upd_len(self):
        try:
            d = float(self.t_end.get()) - float(self.t_start.get())
            self.t_len.configure(text=f"Độ dài: {d:.1f} giây", text_color=self.T["TEXT"] if d >= 3 else "#F87171")
        except (ValueError, AttributeError):
            pass

    # ---------- post caption ----------
    def _post_tab(self, t):
        T, f = self.T, self.app.font
        t.grid_columnconfigure(1, weight=1)
        caps = self.clip.get("captions") or {}
        self.post = {}
        for r, k in enumerate(("TikTok", "Instagram Reels", "YouTube Shorts")):
            tb = ctk.CTkTextbox(t, height=86, fg_color=T["CARD2"], text_color=T["TEXT"], font=f(13), corner_radius=10,
                                wrap="word")
            tb.insert("1.0", caps.get(k, ""))
            self._row(t, r, k, tb)
            self.post[k] = tb
        self.tags = ctk.CTkEntry(t, height=34, corner_radius=8, border_width=0, fg_color=T["CARD2"],
                                 text_color=T["TEXT"], font=f(13))
        self.tags.insert(0, " ".join(self.clip.get("hashtags") or []))
        self._row(t, 3, "Hashtag", self.tags, "Cách nhau bằng dấu cách.")

    # ---------- save / re-render ----------
    def _collect(self):
        c = self.clip
        c["hook_title"] = self.hook_text.get("1.0", "end").strip()
        c["hook_style"] = {"enabled": bool(self.hook_on.get()), "position": dict((v, k) for k, v in POSITIONS)[self.hook_pos.get()],
                           "theme": dict((v, k) for k, v in THEMES)[self.hook_theme.get()],
                           "size": round(self.hook_size.get(), 2),
                           "seconds": "all" if self.hook_all.get() else round(self.hook_secs.get())}
        c["caption_style"] = {"enabled": bool(self.cap_on.get()), "uppercase": bool(self.cap_upper.get()),
                              "highlight": self.cap_color, "size": round(self.cap_size.get(), 2),
                              "y": round(self.cap_y.get(), 3)}
        try:
            s, e = float(self.t_start.get()), float(self.t_end.get())
        except ValueError:
            raise ProjectError("Thời điểm bắt đầu/kết thúc phải là số giây, ví dụ 125.5")
        if e - s < 3:
            raise ProjectError("Clip phải dài ít nhất 3 giây.")
        timing_changed = abs(s - float(c.get("start", s))) > 0.05 or abs(e - float(c.get("end", e))) > 0.05
        c.pop("ranges", None)
        c["start"], c["end"] = s, e
        if timing_changed:
            c.pop("captions_override", None)
        elif self.cap_rows:
            edited = [{"s": ch["s"], "e": ch["e"], "text": ent.get().strip()} for ch, ent in self.cap_rows]
            auto = default_chunks(self.rendered, self.words)
            if [x["text"] for x in edited] != [x["text"] for x in auto]:
                c["captions_override"] = edited
            else:
                c.pop("captions_override", None)
        c["captions"] = {k: tb.get("1.0", "end").strip() for k, tb in self.post.items()}
        c["hashtags"] = [h if h.startswith("#") else "#" + h for h in self.tags.get().split()]
        return c

    def _save(self):
        try:
            clip = self._collect()
        except ProjectError as e:
            self.msg.configure(text=str(e), text_color="#F87171")
            return
        self.save_btn.configure(state="disabled", text="Đang dựng lại...")
        self.msg.configure(text="Đang dựng lại clip, thường mất 20–60 giây...", text_color=self.T["MUTED"])

        def work():
            try:
                new = rerender(self.out_dir, self.index, clip, log=lambda m: None)
                self.after(0, lambda: self._done(new, None))
            except Exception as e:
                self.after(0, lambda e=e: self._done(None, str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _done(self, new, err):
        self.save_btn.configure(state="normal", text="Dựng lại clip")
        if err:
            self.msg.configure(text=f"Lỗi: {err}", text_color="#F87171")
            return
        self.rendered = new
        self._show_thumb()
        self.msg.configure(text="Đã dựng lại! Bấm Xem clip để kiểm tra.", text_color="#4ADE80")
        self.on_saved(self.index, self.clip, new)
