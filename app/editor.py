"""Manual editing of one rendered clip with a live preview: drag the hook and captions on the picture, scroll to
resize, drag the picture to re-aim the vertical crop; typography presets, timing, audio, post captions."""
import contextlib, io, json, os, threading, tkinter as tk

import customtkinter as ctk

from .engine import render_clips
from .preview import Preview, W as OW, H as OH


class ProjectError(Exception):
    pass


def load_project(out_dir):
    pj = os.path.join(out_dir, "project.json")
    if not os.path.exists(pj):
        raise ProjectError("Dự án này được tạo bằng bản cũ nên không chỉnh sửa được. Hãy tạo lại clip.")
    proj = json.load(open(pj, encoding="utf-8"))
    old = proj.get("out_dir")
    if old and os.path.normcase(os.path.abspath(old)) != os.path.normcase(os.path.abspath(out_dir)):
        # the project folder was renamed or moved: re-point every stored path
        def fix(v):
            if isinstance(v, str) and v.startswith(old):
                return out_dir + v[len(old):]
            return [fix(x) for x in v] if isinstance(v, list) else v
        proj = {k: fix(v) for k, v in proj.items()}
    if not os.path.exists(proj.get("video", "")):
        raise ProjectError("Không còn video gốc của dự án này (đã dọn file tạm hoặc file bị xoá/di chuyển), "
                           "nên không dựng lại được. Hãy tạo lại clip.")
    data = json.load(open(os.path.join(out_dir, "clips_rendered.json"), encoding="utf-8"))
    return proj, data


def transcript_words(proj):
    t = proj.get("transcript")
    if t and os.path.exists(t):
        return render_clips.all_words(json.load(open(t, encoding="utf-8")))
    return []


def default_chunks(rendered, words):
    rs = rendered.get("pieces") or rendered["ranges"]
    cw, _ = render_clips.clip_words_of([tuple(r) for r in rs], words)
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
THEMES = list(render_clips.HOOK_THEME_LABELS.items())
FONTS = list(render_clips.HOOK_FONTS)
COLORS = ["", "FFE600", "22D3EE", "FF4FA3", "5BFF6B", "FF7A00", "FFFFFF"]
UPPER = [("auto", "Theo mẫu"), ("on", "IN HOA"), ("off", "Thường")]
CW, CH = 315, 560          # preview canvas size (9:16)
SC = CW / OW               # output pixels -> canvas pixels


def _fmt(t):
    m, s = divmod(max(0.0, float(t)), 60)
    return f"{int(m):02d}:{s:04.1f}"


def _k(pairs, label):
    return next((k for k, v in pairs if v == label), pairs[0][0])


def _v(pairs, key):
    return dict(pairs).get(key, pairs[0][1])


class ClipEditor(ctk.CTkToplevel):
    """`app` is the main window (fonts, colours, helpers); `on_saved(index, clip, rendered)` refreshes the grid."""

    def __init__(self, app, out_dir, index, on_saved):
        super().__init__(app)
        self.app, self.out_dir, self.index, self.on_saved = app, out_dir, index, on_saved
        from . import tools
        tools.use_installed()
        self.proj, data = load_project(out_dir)
        self.clip = json.loads(json.dumps(data["clips"][index]))
        self.rendered = data["rendered"][index]
        self.words = transcript_words(self.proj)
        self.pv = Preview(self.proj, self.words)
        self.D = self.pv.defaults
        T = app.theme
        self.T = T
        self.title(f"Chỉnh sửa · {self.clip.get('title', 'Clip')}")
        self.geometry("1260x860")
        self.minsize(1100, 760)
        self.configure(fg_color=T["BG"])
        self.transient(app)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        hs = self.clip.get("hook_style") or {}
        self.hook_y = hs.get("y")
        self.t = 1.0
        self._job, self._busy, self._dirty, self._photo = None, False, False, None
        self._handles, self._info, self._drag, self._box0 = {}, {}, None, None
        self._build_preview()
        self._build_tabs()
        self.after(50, self.lift)
        self.after(120, self.refresh)

    # ================= live preview =================
    def _build_preview(self):
        T, f = self.T, self.app.font
        left = ctk.CTkFrame(self, fg_color=T["CARD"], corner_radius=16, border_width=1, border_color=T["BORDER"])
        left.grid(row=0, column=0, sticky="ns", padx=(18, 9), pady=18)
        self.canvas = tk.Canvas(left, width=CW, height=CH, bg="#000000", highlightthickness=0, cursor="hand2")
        self.canvas.grid(row=0, column=0, columnspan=2, padx=16, pady=(16, 6))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-Button-1>", self._dbl)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda e: self._wheel(e, 120))
        self.canvas.bind("<Button-5>", lambda e: self._wheel(e, -120))
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Left>", lambda e: self._step(-0.5))
        self.canvas.bind("<Right>", lambda e: self._step(0.5))
        self.loading = self.canvas.create_text(CW / 2, CH / 2, text="Đang dựng xem trước...", fill="#9CA0AB",
                                               font=("Segoe UI", 11))
        tl = ctk.CTkFrame(left, fg_color="transparent")
        tl.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16)
        tl.grid_columnconfigure(1, weight=1)
        self.app.button(tl, "◀", lambda: self._step(-1), width=30, height=28).grid(row=0, column=0)
        self.scrub = ctk.CTkSlider(tl, from_=0, to=max(1.0, float(self.rendered.get("length", 30))),
                                   progress_color=T["ACCENT"], button_color=T["TEXT"], button_hover_color=T["TEXT"],
                                   command=self._scrubbed)
        self.scrub.set(self.t)
        self.scrub.grid(row=0, column=1, sticky="ew", padx=6)
        self.app.button(tl, "▶", lambda: self._step(1), width=30, height=28).grid(row=0, column=2)
        self.t_lbl = ctk.CTkLabel(tl, text=_fmt(self.t), font=f(12), text_color=T["MUTED"], width=46)
        self.t_lbl.grid(row=0, column=3, padx=(6, 0))
        # framing of the shot under the playhead
        fr = ctk.CTkFrame(left, fg_color=T["CARD2"], corner_radius=10)
        fr.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(8, 4))
        fr.grid_columnconfigure(0, weight=1)
        self.shot_lbl = ctk.CTkLabel(fr, text="", font=f(12), text_color=T["TEXT"], anchor="w", justify="left",
                                     wraplength=300)
        self.shot_lbl.grid(row=0, column=0, columnspan=6, sticky="w", padx=10, pady=(6, 2))
        self.lay_btn = {}
        for i, (k, txt) in enumerate((("fill", "Lấp đầy"), ("fit", "Nền mờ"))):
            b = ctk.CTkButton(fr, text=txt, width=66, height=26, corner_radius=8, font=f(12),
                              command=lambda k=k: self._set_shot(layout=k))
            b.grid(row=1, column=i, padx=(10 if i == 0 else 2, 2), pady=(0, 8))
            self.lay_btn[k] = b
        self.zoom_btn = {}
        for i, z in enumerate((1.0, 1.2, 1.4)):
            b = ctk.CTkButton(fr, text=f"{z:g}x", width=40, height=26, corner_radius=8, font=f(12),
                              command=lambda z=z: self._set_shot(zoom=z))
            b.grid(row=1, column=2 + i, padx=2, pady=(0, 8))
            self.zoom_btn[z] = b
        ctk.CTkButton(fr, text="↺", width=28, height=26, corner_radius=8, font=f(13), fg_color="transparent",
                      hover_color=T["BORDER"], text_color=T["ACCENT"], command=self._reset_shot).grid(
            row=1, column=5, padx=(2, 8), pady=(0, 8))
        ctk.CTkLabel(left, text="Kéo tiêu đề / phụ đề để di chuyển · lăn chuột để đổi cỡ\nKéo ngang hình để dời khung "
                                "· nhấp đúp: Lấp đầy ↔ Nền mờ", font=f(11), text_color=T["MUTED"], justify="left"
                     ).grid(row=3, column=0, columnspan=2, sticky="w", padx=16)
        self.app.button(left, "▶ Xem clip", lambda: self.app.open_path(os.path.join(self.out_dir, self.rendered["file"])),
                        height=40, width=120).grid(row=4, column=0, sticky="ew", padx=(16, 4), pady=(10, 4))
        self.save_btn = self.app.button(left, "Dựng lại clip", self._save, primary=True, height=40)
        self.save_btn.grid(row=4, column=1, sticky="ew", padx=(4, 16), pady=(10, 4))
        self.msg = ctk.CTkLabel(left, text="Xem trước cập nhật ngay khi bạn sửa. Xong bấm Dựng lại clip.", font=f(12),
                                text_color=T["MUTED"], wraplength=320, justify="left")
        self.msg.grid(row=5, column=0, columnspan=2, sticky="w", padx=16, pady=(4, 14))

    def refresh(self, *_):
        """Debounced: the newest settings win, renders never pile up."""
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(140, self._kick)

    def _kick(self):
        self._job = None
        if self._busy:
            self._dirty = True
            return
        try:
            clip = self._collect(preview=True)
        except Exception:
            return
        chunks = clip.get("captions_override")
        self._busy = True
        t = self.t

        def work():
            try:
                res = self.pv.render(clip, t, chunks)
                self.after(0, lambda: self._show(*res))
            except Exception as e:
                self.after(0, lambda e=e: self._show(None, {}, {"error": str(e)}))
        threading.Thread(target=work, daemon=True).start()

    def _show(self, img, handles, info):
        self._busy = False
        if not self.winfo_exists():
            return
        if img is not None:
            from PIL import ImageTk
            self._photo = ImageTk.PhotoImage(img.resize((CW, CH)))
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
            self._handles, self._info = handles, info
            self._draw_handles()
            self._upd_shot()
            if info.get("total"):
                self.scrub.configure(to=max(1.0, info["total"]))
        elif info.get("error"):
            self.canvas.delete("all")
            self.canvas.create_text(CW / 2, CH / 2, text="Không dựng được xem trước:\n" + info["error"][:200],
                                    fill="#F87171", width=CW - 30)
        if self._dirty:
            self._dirty = False
            self.refresh()

    def _draw_handles(self, hot=None):
        self.canvas.delete("handle")
        for k, box in self._handles.items():
            x0, y0, x1, y1 = (v * SC for v in box)
            on = hot == k or (self._drag and self._drag[0] == k)
            self.canvas.create_rectangle(x0 - 3, y0 - 3, x1 + 3, y1 + 3, outline="#22D3EE" if on else "#FFFFFF",
                                         dash=() if on else (4, 3), width=2 if on else 1, tags="handle")
            if on:
                self.canvas.create_text(x0, y0 - 10, anchor="w", fill="#22D3EE", font=("Segoe UI", 9, "bold"),
                                        text="Tiêu đề hook" if k == "hook" else "Phụ đề", tags="handle")

    def _hit(self, x, y):
        for k in ("hook", "cap"):
            b = self._handles.get(k)
            if b and b[0] * SC - 6 <= x <= b[2] * SC + 6 and b[1] * SC - 6 <= y <= b[3] * SC + 6:
                return k
        return None

    def _hover(self, e):
        k = self._hit(e.x, e.y)
        self.canvas.configure(cursor="fleur" if k else "sb_h_double_arrow")
        self._draw_handles(hot=k)

    def _press(self, e):
        self.canvas.focus_set()
        k = self._hit(e.x, e.y) or "video"
        self._drag = (k, e.x, e.y, self._drag_start_value(k))
        self._box0 = self._handles.get(k)

    def _drag_start_value(self, k):
        if k == "hook":
            return self._handles["hook"][1] / OH
        if k == "cap":
            return self.cap_y.get()
        seg = self._info.get("seg")
        return seg[3] if seg else 0.5

    def _motion(self, e):
        if not self._drag:
            return
        k, x0, y0, v0 = self._drag
        dy, dx = (e.y - y0) / CH, (e.x - x0) / CW
        if k == "hook":
            self.hook_y = round(max(0.0, min(0.9, v0 + dy)), 3)
            self.hook_pos.set("")
        elif k == "cap":
            self.cap_y.set(max(0.2, min(0.97, v0 + dy)))
            self.cap_y_lbl.configure(text=f"{self.cap_y.get() * 100:.0f}% từ trên")
        else:
            seg = self._info.get("seg")
            if not seg or seg[2] == "fit":
                return
            # dragging the picture right moves the window left (like moving a photo under a frame)
            aw, ah = self.pv.act["w"], self.pv.act["h"]
            span = (ah * OW / OH) / aw / max(1.0, seg[4].get("zoom", 1.0))
            self._set_shot(crop_x=round(max(0.0, min(1.0, v0 - dx * span)), 3), refresh=False)
        # move the dashed box right away; the picture follows when the render arrives
        if k in ("hook", "cap") and self._box0:
            b = self._box0
            shift = (e.y - y0) / SC
            self._handles[k] = (b[0], b[1] + shift, b[2], b[3] + shift)
            self._draw_handles(hot=k)
        self.refresh()

    def _release(self, _e):
        self._drag = None
        self._draw_handles()

    def _dbl(self, e):
        if self._hit(e.x, e.y):
            return
        seg = self._info.get("seg")
        if seg:
            self._set_shot(layout="fit" if seg[2] == "fill" else "fill")

    def _wheel(self, e, delta=None):
        d = delta if delta is not None else e.delta
        k = self._hit(e.x, e.y)
        step = 0.05 if d > 0 else -0.05
        if k == "hook":
            self.hook_size.set(max(0.6, min(1.8, self.hook_size.get() + step)))
            self.hook_size_lbl.configure(text=f"{self.hook_size.get() * 100:.0f}%")
        elif k == "cap":
            self.cap_size.set(max(0.6, min(1.8, self.cap_size.get() + step)))
            self.cap_size_lbl.configure(text=f"{self.cap_size.get() * 100:.0f}%")
        else:
            self._step(0.5 if d < 0 else -0.5)
            return
        self.refresh()

    def _scrubbed(self, v):
        self.t = float(v)
        self.t_lbl.configure(text=_fmt(self.t))
        self.refresh()

    def _step(self, d):
        self.t = max(0.0, min(float(self.scrub.cget("to")), self.t + d))
        self.scrub.set(self.t)
        self.t_lbl.configure(text=_fmt(self.t))
        self.refresh()

    # ---------- per-shot framing ----------
    def _shot_key(self):
        info = self._info
        sh = info.get("shot")
        if sh:
            return (sh["start"] + sh["end"]) / 2
        seg = info.get("seg")
        return (seg[0] + seg[1]) / 2 if seg else None

    def _set_shot(self, layout=None, crop_x=None, zoom=None, refresh=True):
        at = self._shot_key()
        seg = self._info.get("seg")
        if at is None or not seg:
            return
        ovs = self.clip.setdefault("layout_overrides", [])
        o = next((x for x in ovs if abs(x["at"] - at) < 1e-3), None)
        if not o:
            o = {"at": round(at, 3)}
            ovs.append(o)
        if layout:
            o["layout"] = layout
        if crop_x is not None:
            o["crop_x"] = crop_x
            o.setdefault("layout", "fill")
        if zoom is not None:
            o["zoom"] = zoom
            o.setdefault("layout", "fill")
        # reflect it in the cached segment so a drag keeps working before the render returns
        ex = dict(seg[4])
        if zoom is not None:
            ex["zoom"] = zoom
        if crop_x is not None:
            ex["track"] = None
        self._info["seg"] = (seg[0], seg[1], layout or o.get("layout", seg[2]),
                             crop_x if crop_x is not None else seg[3], ex)
        self._upd_shot()
        if refresh:
            self.refresh()

    def _reset_shot(self):
        at = self._shot_key()
        if at is None:
            return
        self.clip["layout_overrides"] = [o for o in self.clip.get("layout_overrides") or [] if abs(o["at"] - at) >= 1e-3]
        self.refresh()

    def _upd_shot(self):
        T = self.T
        seg, sh = self._info.get("seg"), self._info.get("shot")
        if not seg:
            return
        lay, ex = seg[2], seg[4] or {}
        why = (sh or {}).get("reason", "")
        focus = {"face": "bám mặt", "speaker": "bám người đang nói", "main face": "bám mặt chính",
                 "close-up": "cận mặt", "person": "bám người", "action": "bám hành động"}.get((sh or {}).get("focus"), "")
        manual = any(abs(o["at"] - (self._shot_key() or -1)) < 1e-3 for o in self.clip.get("layout_overrides") or [])
        txt = f"Cảnh này: {'Lấp đầy' if lay == 'fill' else 'Nền mờ'}"
        if lay == "fill" and ex.get("zoom", 1) > 1.01:
            txt += f" · zoom {ex['zoom']:g}x"
        txt += f" · {'chỉnh tay' if manual else (focus or why[:40])}" if (manual or focus or why) else ""
        self.shot_lbl.configure(text=txt)
        for k, b in self.lay_btn.items():
            b.configure(fg_color=T["ACCENT"] if k == lay else T["CARD"], text_color="#03161A" if k == lay else T["TEXT"],
                        hover_color=T["ACCENT_HOVER"] if k == lay else T["BORDER"])
        z = ex.get("zoom", 1.0) or 1.0
        for k, b in self.zoom_btn.items():
            on = abs(k - z) < 0.05 and lay == "fill"
            b.configure(fg_color=T["ACCENT"] if on else T["CARD"], text_color="#03161A" if on else T["TEXT"],
                        hover_color=T["ACCENT_HOVER"] if on else T["BORDER"])

    # ================= tabs =================
    def _build_tabs(self):
        T = self.T
        tabs = ctk.CTkTabview(self, fg_color=T["CARD"], segmented_button_selected_color=T["ACCENT"],
                              segmented_button_selected_hover_color=T["ACCENT_HOVER"],
                              segmented_button_unselected_color=T["CARD2"], text_color=T["TEXT"],
                              corner_radius=16, border_width=1, border_color=T["BORDER"])
        tabs.grid(row=0, column=1, sticky="nsew", padx=(9, 18), pady=18)
        self._caption_tab(tabs.add("Phụ đề"))
        self._hook_tab(tabs.add("Tiêu đề hook"))
        self._time_tab(tabs.add("Thời lượng & nhịp"))
        self._audio_tab(tabs.add("Âm thanh"))
        self._post_tab(tabs.add("Caption đăng bài"))

    def _row(self, master, r, label, widget, hint=None):
        f = self.app.font
        ctk.CTkLabel(master, text=label, font=f(13), text_color=self.T["MUTED"], anchor="w").grid(
            row=r, column=0, sticky="nw", padx=(16, 12), pady=7)
        widget.grid(row=r, column=1, sticky="ew", padx=(0, 16), pady=7)
        if hint:
            ctk.CTkLabel(master, text=hint, font=f(11), text_color=self.T["MUTED"], anchor="w", justify="left",
                         wraplength=520).grid(row=r + 1, column=1, sticky="w", padx=(0, 16))

    def _slider(self, master, frm, to, value, steps, fmt):
        box = ctk.CTkFrame(master, fg_color="transparent")
        box.grid_columnconfigure(0, weight=1)
        lbl = ctk.CTkLabel(box, text=fmt(value), font=self.app.font(13), text_color=self.T["TEXT"], width=90)

        def moved(v):
            lbl.configure(text=fmt(v))
            self.refresh()
        s = ctk.CTkSlider(box, from_=frm, to=to, number_of_steps=steps, progress_color=self.T["ACCENT"],
                          button_color=self.T["TEXT"], button_hover_color=self.T["TEXT"], command=moved)
        s.set(value)
        s.grid(row=0, column=0, sticky="ew")
        lbl.grid(row=0, column=1, padx=(10, 0))
        return box, s, lbl

    def _switch(self, master, text, on):
        s = ctk.CTkSwitch(master, text=text, font=self.app.font(13), text_color=self.T["TEXT"],
                          progress_color=self.T["ACCENT"], command=self.refresh)
        (s.select if on else s.deselect)()
        return s

    def _seg(self, master, pairs, key):
        s = self.app.seg(master, pairs, key, lambda _: self.refresh())
        return s

    # ---------- captions ----------
    def _caption_tab(self, t):
        T, f = self.T, self.app.font
        t.grid_columnconfigure(1, weight=1)
        t.grid_rowconfigure(9, weight=1)
        cs = self.clip.get("caption_style") or {}
        self.cap_on = self._switch(t, "Hiện phụ đề", cs.get("enabled", self.D["captions"]))
        self.cap_on.grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 4))
        # typography gallery
        gal = ctk.CTkFrame(t, fg_color="transparent")
        gal.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12)
        self.preset = cs.get("preset", self.D["preset"])
        self.preset_btn, self._preset_imgs = {}, []
        from PIL import Image
        from . import config
        for i, (k, P) in enumerate(render_clips.CAPTION_PRESETS.items()):
            try:
                im = Image.open(config.resource_path("assets", "presets", f"{k}.png"))
                img = ctk.CTkImage(im, im, size=(128, 39))
                self._preset_imgs.append(img)
            except Exception:
                img = None
            cell = ctk.CTkFrame(gal, fg_color="transparent")
            cell.grid(row=i // 5, column=i % 5, padx=2, pady=2)
            b = ctk.CTkButton(cell, text="", image=img, width=134, height=46, corner_radius=10, fg_color=T["CARD2"],
                              hover_color=T["BORDER"], border_width=2, command=lambda k=k: self._pick_preset(k))
            b.grid(row=0, column=0)
            ctk.CTkLabel(cell, text=P["label"], font=f(11), text_color=T["MUTED"]).grid(row=1, column=0)
            self.preset_btn[k] = b
        self._pick_preset(self.preset, refresh=False)
        top = ctk.CTkFrame(t, fg_color="transparent")
        top.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(6, 2))
        ctk.CTkLabel(top, text="Màu nhấn", font=f(13), text_color=T["MUTED"]).grid(row=0, column=0, padx=(0, 10))
        self.cap_color = (cs.get("highlight") or "").lstrip("#")
        self.cap_sw = {}
        for i, c in enumerate(COLORS):
            b = ctk.CTkButton(top, text="A" if not c else "", width=26, height=26, corner_radius=13,
                              fg_color=f"#{c}" if c else T["CARD2"], hover_color=f"#{c}" if c else T["BORDER"],
                              text_color=T["TEXT"], font=f(11, "bold"), border_width=3,
                              command=lambda c=c: self._cap_pick(c))
            b.grid(row=0, column=1 + i, padx=(0, 6))
            self.cap_sw[c] = b
        self._cap_pick(self.cap_color, refresh=False)
        up = cs.get("uppercase")
        self.cap_upper = self._seg(top, UPPER, "auto" if up is None else ("on" if up else "off"))
        self.cap_upper.grid(row=0, column=9, padx=(14, 0))
        self.cap_emph = self._switch(top, "Tô màu từ khoá", cs.get("emphasis", self.D["emphasis"]))
        self.cap_emph.grid(row=0, column=10, padx=(14, 0))
        box, self.cap_size, self.cap_size_lbl = self._slider(t, 0.6, 1.8, float(cs.get("size", 1.0)), 24,
                                                             lambda v: f"{v * 100:.0f}%")
        self._row(t, 3, "Cỡ chữ", box)
        box, self.cap_y, self.cap_y_lbl = self._slider(t, 0.2, 0.97, float(cs.get("y", 0.75)), 77,
                                                       lambda v: f"{v * 100:.0f}% từ trên")
        self._row(t, 4, "Vị trí dọc", box)
        ctk.CTkLabel(t, text="Sửa chữ từng dòng (để trống một dòng để ẩn dòng đó):", font=f(13),
                     text_color=T["MUTED"], anchor="w").grid(row=7, column=0, columnspan=2, sticky="w", padx=16,
                                                             pady=(6, 4))
        ctk.CTkButton(t, text="Khôi phục phụ đề gốc", height=26, width=150, fg_color="transparent",
                      hover_color=T["CARD2"], text_color=T["ACCENT"], font=f(12), command=self._reset_caps).grid(
            row=7, column=1, sticky="e", padx=16)
        self.cap_list = ctk.CTkScrollableFrame(t, fg_color=T["CARD2"], corner_radius=10, height=150)
        self.cap_list.grid(row=9, column=0, columnspan=2, sticky="nsew", padx=16, pady=(0, 12))
        self.cap_list.grid_columnconfigure(1, weight=1)
        self._fill_caps(self.clip.get("captions_override") or self._auto_chunks())

    def _pick_preset(self, k, refresh=True):
        self.preset = k if k in render_clips.CAPTION_PRESETS else "classic"
        for key, b in self.preset_btn.items():
            b.configure(border_color=self.T["ACCENT"] if key == self.preset else self.T["CARD2"])
        if refresh:
            self.refresh()

    def _cap_pick(self, c, refresh=True):
        self.cap_color = c
        for k, b in self.cap_sw.items():
            b.configure(border_color=self.T["TEXT"] if k == c else self.T["CARD"])
        if refresh:
            self.refresh()

    def _fill_caps(self, chunks):
        for w in self.cap_list.winfo_children():
            w.destroy()
        self.cap_rows = []
        if not chunks:
            ctk.CTkLabel(self.cap_list, text="Clip này không có lời nói nên không có phụ đề.", font=self.app.font(13),
                         text_color=self.T["MUTED"]).grid(row=0, column=0, columnspan=2, pady=20)
            return
        for i, c in enumerate(chunks):
            lbl = ctk.CTkButton(self.cap_list, text=_fmt(c["s"]), font=self.app.font(12), text_color=self.T["ACCENT"],
                                width=56, height=26, fg_color="transparent", hover_color=self.T["CARD"],
                                command=lambda s=c["s"]: self._goto(s + 0.05))
            lbl.grid(row=i, column=0, padx=(4, 6), pady=2)
            e = ctk.CTkEntry(self.cap_list, height=30, corner_radius=8, border_width=0, fg_color=self.T["CARD"],
                             text_color=self.T["TEXT"], font=self.app.font(14))
            e.insert(0, c["text"])
            e.grid(row=i, column=1, sticky="ew", padx=(0, 6), pady=2)
            e.bind("<FocusIn>", lambda _e, s=c["s"]: self._goto(s + 0.05))
            e.bind("<KeyRelease>", self.refresh)
            self.cap_rows.append((c, e))

    def _goto(self, t):
        self.t = t
        self.scrub.set(t)
        self.t_lbl.configure(text=_fmt(t))
        self.refresh()

    def _auto_chunks(self):
        """The captions a reset goes back to: the localized lines for a market version, else the speech."""
        return self.clip.get("captions_localized") or default_chunks(self.rendered, self.words)

    def _reset_caps(self):
        self.clip.pop("captions_override", None)
        if self.clip.get("captions_localized"):
            self.clip["captions_override"] = self.clip["captions_localized"]
        self._fill_caps(self._auto_chunks())
        self.refresh()

    # ---------- hook ----------
    def _hook_tab(self, t):
        T, f, app = self.T, self.app.font, self.app
        t.grid_columnconfigure(1, weight=1)
        hs = self.clip.get("hook_style") or {}
        self.hook_on = self._switch(t, "Hiện tiêu đề hook", hs.get("enabled", self.D["hook"]))
        self.hook_on.grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(14, 6))
        self.hook_text = ctk.CTkTextbox(t, height=90, fg_color=T["CARD2"], text_color=T["TEXT"], font=f(16, "bold"),
                                        corner_radius=10, wrap="word")
        self.hook_text.insert("1.0", self.clip.get("hook_title", ""))
        self.hook_text.bind("<KeyRelease>", self.refresh)
        self.hook_text.bind("<FocusIn>", lambda _e: self._goto(min(self.t, 1.0)))
        self._row(t, 1, "Nội dung", self.hook_text, "Enter để tự xuống dòng. Nên ≤ 8 chữ: cụ thể, gây tò mò, "
                                                    "đúng nội dung clip.")
        self.hook_font = ctk.CTkOptionMenu(t, values=FONTS, height=32, corner_radius=8, fg_color=T["CARD2"],
                                           button_color=T["CARD2"], button_hover_color=T["BORDER"],
                                           dropdown_fg_color=T["CARD2"], text_color=T["TEXT"], font=f(13),
                                           command=self.refresh)
        self.hook_font.set(hs.get("font", "Inter") if hs.get("font", "Inter") in FONTS else "Inter")
        self._row(t, 3, "Phông chữ", self.hook_font)
        self.hook_theme = ctk.CTkOptionMenu(t, values=[v for _, v in THEMES], height=32, corner_radius=8,
                                            fg_color=T["CARD2"], button_color=T["CARD2"],
                                            button_hover_color=T["BORDER"], dropdown_fg_color=T["CARD2"],
                                            text_color=T["TEXT"], font=f(13), command=self.refresh)
        self.hook_theme.set(_v(THEMES, hs.get("theme", "light")))
        self._row(t, 4, "Kiểu khung", self.hook_theme)
        self.hook_upper = self._switch(t, "IN HOA", hs.get("uppercase", True))
        self._row(t, 5, "Chữ", self.hook_upper)
        box, self.hook_size, self.hook_size_lbl = self._slider(t, 0.6, 1.8, float(hs.get("size", 1.0)), 24,
                                                               lambda v: f"{v * 100:.0f}%")
        self._row(t, 6, "Cỡ chữ", box)

        def pos(_):
            self.hook_y = None
            self.refresh()
        self.hook_pos = app.seg(t, POSITIONS, hs.get("position", "top"), pos)
        if self.hook_y is not None:
            self.hook_pos.set("")
        self._row(t, 7, "Vị trí", self.hook_pos, "Hoặc kéo trực tiếp thẻ tiêu đề trên khung xem trước.")
        length = float(self.rendered["length"])
        secs = hs.get("seconds", self.clip.get("hook_seconds", 4.0))
        allc = secs in ("all", None, 0)
        box, self.hook_secs, _ = self._slider(t, 1, max(2, min(30, length)), 4.0 if allc else float(secs),
                                              int(max(2, min(30, length))) - 1, lambda v: f"{v:.0f} giây")
        self._row(t, 9, "Hiện trong", box)
        self.hook_all = self._switch(t, "Hiện suốt cả clip", allc)
        self.hook_all.grid(row=10, column=1, sticky="w", padx=(0, 16), pady=(0, 8))

    # ---------- timing & pacing ----------
    def _time_tab(self, t):
        T, f = self.T, self.app.font
        t.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(t, text="Thời điểm bắt đầu và kết thúc trong video gốc (giây). Đổi thời lượng sẽ khôi phục "
                             "phụ đề về bản gốc vì mốc thời gian thay đổi.", font=f(12), text_color=T["MUTED"],
                     wraplength=600, justify="left", anchor="w").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                      padx=16, pady=(14, 8))
        self.t_start = self._time_row(t, 1, "Bắt đầu", float(self.clip.get("start", self.rendered["ranges"][0][0])))
        self.t_end = self._time_row(t, 2, "Kết thúc", float(self.clip.get("end", self.rendered["ranges"][-1][1])))
        self.t_len = ctk.CTkLabel(t, text="", font=f(13), text_color=T["TEXT"], anchor="w")
        self.t_len.grid(row=3, column=1, sticky="w", pady=8)
        self._upd_len()
        ctk.CTkFrame(t, height=1, fg_color=T["BORDER"]).grid(row=4, column=0, columnspan=2, sticky="ew", padx=16,
                                                             pady=10)
        self.jump = self._switch(t, "Cắt khoảng lặng & từ đệm (ừm, à, uh...)",
                                 self.clip.get("jumpcut", self.D["jumpcut"]))
        self._row(t, 5, "Nhịp dựng", self.jump, "Bỏ chỗ ngập ngừng giữa các câu cho nhịp nhanh kiểu TikTok. Tắt nếu "
                                               "clip là nhạc hoặc cần giữ nguyên khoảng nghỉ.")
        self.pbar = self._switch(t, "Thanh tiến trình chạy dưới đáy video",
                                 self.clip.get("progress_bar", self.D["progress_bar"]))
        self._row(t, 7, "", self.pbar, "Người xem thấy còn bao lâu là hết nên dễ xem trọn clip.")
        jc = self.rendered.get("jump_cuts")
        if jc:
            ctk.CTkLabel(t, text=f"Lần dựng trước đã cắt {jc} khoảng lặng.", font=f(12), text_color=T["MUTED"]).grid(
                row=9, column=1, sticky="w", padx=(0, 16))

    def _time_row(self, t, r, label, value):
        box = ctk.CTkFrame(t, fg_color="transparent")
        e = ctk.CTkEntry(box, width=110, height=34, corner_radius=8, border_width=0, fg_color=self.T["CARD2"],
                         text_color=self.T["TEXT"], font=self.app.font(14))
        e.insert(0, f"{value:.1f}")
        e.bind("<KeyRelease>", lambda _: (self._upd_len(), self.refresh()))

        def nudge(d):
            try:
                v = max(0.0, float(e.get()) + d)
            except ValueError:
                return
            e.delete(0, "end")
            e.insert(0, f"{v:.1f}")
            self._upd_len()
            self.refresh()
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

    # ---------- audio ----------
    def _audio_tab(self, t):
        T, f = self.T, self.app.font
        t.grid_columnconfigure(1, weight=1)
        au = self.clip.get("audio") or {}
        self.au_norm = ctk.CTkSwitch(t, text="Tự cân bằng âm lượng (chuẩn -14 LUFS của TikTok/Reels/Shorts)",
                                     font=f(13), text_color=T["TEXT"], progress_color=T["ACCENT"])
        (self.au_norm.select if au.get("normalize", True) else self.au_norm.deselect)()
        self.au_norm.grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(16, 8))
        box, self.au_gain, _ = self._slider(t, -12, 12, float(au.get("gain_db", 0.0)), 48, lambda v: f"{v:+.1f} dB")
        self._row(t, 1, "Tăng / giảm", box, "Kéo sang phải nếu clip còn nhỏ tiếng, sang trái nếu bị rè/vỡ tiếng. "
                                            "Tắt cân bằng để giữ nguyên âm lượng gốc.")
        ctk.CTkLabel(t, text="Đầu clip bị hụt tiếng hoặc có tiếng thừa? Vào tab Thời lượng, chỉnh Bắt đầu sớm hoặc "
                             "muộn hơn 0,5–1 giây.", font=f(12), text_color=T["MUTED"], wraplength=600, justify="left",
                     anchor="w").grid(row=6, column=0, columnspan=2, sticky="w", padx=16, pady=(14, 0))

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

    # ================= save / re-render =================
    def _collect(self, preview=False):
        jc_changed = bool(self.jump.get()) != bool(self.clip.get("jumpcut", self.D["jumpcut"]))
        c = self.clip if not preview else json.loads(json.dumps(self.clip))
        c["hook_title"] = self.hook_text.get("1.0", "end").strip()
        hs = {"enabled": bool(self.hook_on.get()), "theme": _k(THEMES, self.hook_theme.get()),
              "font": self.hook_font.get(), "uppercase": bool(self.hook_upper.get()),
              "size": round(self.hook_size.get(), 2), "seconds": "all" if self.hook_all.get() else round(self.hook_secs.get())}
        if self.hook_y is not None:
            hs["y"] = self.hook_y
        else:
            hs["position"] = _k(POSITIONS, self.hook_pos.get() or "Trên")
        c["hook_style"] = hs
        cs = {"enabled": bool(self.cap_on.get()), "preset": self.preset, "emphasis": bool(self.cap_emph.get()),
              "size": round(self.cap_size.get(), 2), "y": round(self.cap_y.get(), 3)}
        if self.cap_color:
            cs["highlight"] = self.cap_color
        up = _k(UPPER, self.cap_upper.get())
        if up != "auto":
            cs["uppercase"] = up == "on"
        c["caption_style"] = cs
        c["jumpcut"], c["progress_bar"] = bool(self.jump.get()), bool(self.pbar.get())
        try:
            s, e = float(self.t_start.get()), float(self.t_end.get())
        except ValueError:
            if preview:
                s, e = float(c.get("start", 0)), float(c.get("end", 0))
            else:
                raise ProjectError("Thời điểm bắt đầu/kết thúc phải là số giây, ví dụ 125.5")
        if e - s < 3:
            if preview:
                s, e = float(self.clip.get("start", s)), float(self.clip.get("end", e))
            else:
                raise ProjectError("Clip phải dài ít nhất 3 giây.")
        timing_changed = abs(s - float(self.clip.get("start", s))) > 0.05 or abs(e - float(self.clip.get("end", e))) > 0.05
        c.pop("ranges", None)
        c["start"], c["end"] = s, e
        localized = c.get("captions_localized")
        if (timing_changed or jc_changed) and not localized:
            c.pop("captions_override", None)
        elif self.cap_rows:
            edited = [dict(ch, text=ent.get().strip()) for ch, ent in self.cap_rows]
            auto = self._auto_chunks()
            if [x["text"] for x in edited] != [x["text"] for x in auto] or localized:
                c["captions_override"] = edited
            else:
                c.pop("captions_override", None)
        c["audio"] = {"normalize": bool(self.au_norm.get()), "gain_db": round(self.au_gain.get(), 1)}
        c["zoom_fx"] = "off"
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
        self.msg.configure(text="Đã dựng lại! Bấm Xem clip để kiểm tra.", text_color="#4ADE80")
        self.on_saved(self.index, self.clip, new)
