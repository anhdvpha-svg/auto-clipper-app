"""'Khám phá nguồn' page: find videos worth clipping, then queue them into the clipper."""
import io, os, threading, tkinter as tk

import customtkinter as ctk

from . import config, research, tools

MODES = ["Tìm theo từ khoá", "Kênh theo dõi"]
PERIODS = [("hour", "1 giờ"), ("today", "Hôm nay"), ("week", "Tuần này"), ("month", "Tháng này"), ("year", "Năm nay"),
           ("any", "Mọi lúc")]
LENGTHS = [("long", "Dài >20 phút"), ("medium", "4–20 phút"), ("short", "Ngắn <4 phút"), ("any", "Mọi độ dài")]
SORTS = [("hot", "Điểm Hot"), ("velocity", "View/giờ"), ("outlier", "Vượt trội"), ("views", "Lượt xem"),
         ("date", "Mới nhất")]
COUNTS = [("10", "10"), ("20", "20"), ("40", "40")]
PLAT_COLORS = {"YouTube": "#EF4444", "YouTube Shorts": "#F43F5E", "TikTok": "#111827", "Instagram": "#C026D3",
               "Twitch": "#9146FF", "Kick": "#53FC18", "Dailymotion": "#0066DC", "Bilibili": "#00A1D6",
               "X": "#000000", "Rumble": "#85C742"}
PER_CHANNEL = [("5", "5"), ("10", "10"), ("20", "20")]
TAG_COLORS = {"Vượt trội": "#DC2626", "Đang lên": "#F97316", "Mới": "#22C55E", "Dài": "#64748B", "Đang live": "#EF4444", "Đã cắt": "#6366F1"}


def _lbl(pairs, key):
    return dict(pairs).get(key, pairs[0][1])


def _key(pairs, label):
    return next((k for k, v in pairs if v == label), pairs[0][0])


class ResearchMixin:
    """Mixed into the main App: uses its fonts, theme, seg/button helpers, config and the clip queue."""

    def _build_research(self):
        T = self.theme
        page = ctk.CTkScrollableFrame(self, fg_color=T["BG"], corner_radius=0)
        page.grid_columnconfigure(0, weight=1)
        self.pages["research"] = page
        wrap = ctk.CTkFrame(page, fg_color="transparent")
        wrap.grid(row=0, column=0, sticky="ew", padx=36, pady=(30, 30))
        wrap.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(wrap, text="Khám phá nguồn", font=self.font(28, "bold"), text_color=T["TEXT"],
                     anchor="w").grid(row=0, column=0, sticky="w")
        self.muted(wrap, "Tìm video đang hot theo từ khoá hoặc quét kênh của creator trong campaign, rồi đưa thẳng "
                         "vào hàng chờ cắt clip. Chỉ cắt nội dung mà campaign cho phép.", 14,
                   wraplength=900).grid(
            row=1, column=0, sticky="w", pady=(4, 14))

        self.rs_mode = ctk.CTkSegmentedButton(wrap, values=MODES, font=self.font(14), height=38,
                                              selected_color=T["ACCENT"], selected_hover_color=T["ACCENT_HOVER"],
                                              unselected_color=T["CARD2"], fg_color=T["CARD2"], text_color=T["TEXT"],
                                              corner_radius=10, command=lambda _: self._rs_switch())
        self.rs_mode.set(MODES[0])
        self.rs_mode.grid(row=2, column=0, sticky="w")

        # keyword search card
        kw = self.card(wrap)
        kw.grid_columnconfigure(0, weight=1)
        self.rs_query = ctk.CTkEntry(kw, height=48, corner_radius=12, border_width=0, fg_color=T["CARD2"],
                                     font=self.font(15), text_color=T["TEXT"], placeholder_text_color=T["MUTED"],
                                     placeholder_text="Từ khoá: project, creator, trend... nhiều từ khoá cách nhau "
                                                      "bằng dấu phẩy (vd: bitcoin podcast, crypto news)")
        self.rs_query.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=(12, 8))
        self.rs_query.bind("<Return>", lambda _: self._rs_search())
        self.rs_go = self.button(kw, "Tìm  →", self._rs_search, primary=True, height=48, width=120)
        self.rs_go.grid(row=0, column=1, padx=(0, 12), pady=(12, 8))
        rcfg = self.cfg.setdefault("research", {})
        self.muted(kw, "Quét trên nền tảng (chọn một hoặc nhiều)", 11).grid(row=1, column=0, columnspan=2, sticky="w",
                                                                         padx=14)
        chips = ctk.CTkFrame(kw, fg_color="transparent")
        chips.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(2, 4))
        self.rs_plats = set(rcfg.get("platforms") or ["yt_long"])
        self.rs_chip = {}
        for i, (k, P) in enumerate(research.PLATFORMS.items()):
            b = ctk.CTkButton(chips, text=P["label"], height=30, corner_radius=15, font=self.font(12, "bold"),
                              border_width=1, command=lambda k=k: self._rs_toggle_plat(k))
            b.grid(row=i // 6, column=i % 6, padx=(0, 6), pady=3, sticky="w")
            self.rs_chip[k] = b
        self.rs_plat_hint = self.muted(kw, "", 11, wraplength=880)
        self.rs_plat_hint.grid(row=3, column=0, columnspan=2, sticky="w", padx=14)
        lg = ctk.CTkFrame(kw, fg_color="transparent")
        lg.grid(row=5, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        self.muted(lg, "Nền tảng cần đăng nhập (đăng nhập 1 lần, app nhớ phiên):", 11).grid(row=0, column=0,
                                                                                          sticky="w", padx=2)
        self.login_buttons(lg, ["tiktok", "instagram", "x", "facebook", "twitch"], compact=True).grid(
            row=1, column=0, sticky="w")
        self._login_refreshers = getattr(self, "_login_refreshers", []) + [lambda: self._rs_toggle_plat(None)]
        self._rs_toggle_plat(None)
        filt = ctk.CTkFrame(kw, fg_color="transparent")
        filt.grid(row=4, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 4))
        self.rs_period = self._rs_filter(filt, 0, "Đăng trong", PERIODS, rcfg.get("period", "week"))
        self.rs_len = self._rs_filter(filt, 1, "Độ dài", LENGTHS, rcfg.get("length", "any"))
        self.rs_count = self._rs_filter(filt, 2, "Kết quả mỗi nền tảng", COUNTS, str(rcfg.get("count", 20)))
        self.rs_kw_card = kw

        # viral filters (both modes): applied instantly to the results
        vf = self.card(wrap)
        vf.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.rs_vf_card = vf
        self.muted(vf, "Bộ lọc viral (áp dụng ngay lên kết quả)", 12).grid(row=0, column=0, columnspan=6, sticky="w",
                                                                         padx=14, pady=(10, 2))
        self.rs_sort = self._rs_filter(vf, 0, "Sắp xếp theo", SORTS, rcfg.get("order", "hot"), row=1)
        self.rs_sort.configure(command=lambda _: self._rs_apply())
        self.rs_sort.master.grid_configure(padx=(12, 18), columnspan=3)

        def entry(col, title, key, ph, width=110):
            box = ctk.CTkFrame(vf, fg_color="transparent")
            box.grid(row=2, column=col, sticky="w", padx=(12 if col == 0 else 0, 14), pady=(0, 12))
            self.muted(box, title, 11).grid(row=0, column=0, sticky="w")
            e = ctk.CTkEntry(box, width=width, height=30, corner_radius=8, border_width=0, fg_color=T["CARD2"],
                             font=self.font(12), text_color=T["TEXT"], placeholder_text=ph,
                             placeholder_text_color=T["MUTED"])
            if rcfg.get(key):
                e.insert(0, str(rcfg[key]))
            e.grid(row=1, column=0, sticky="w")
            e.bind("<KeyRelease>", lambda _: self._rs_apply_later())
            return e
        self.rs_minv = entry(0, "Lượt xem tối thiểu", "min_views", "vd: 50000")
        self.rs_minvph = entry(1, "View/giờ tối thiểu", "min_vph", "vd: 1000")
        self.rs_minhot = entry(2, "Điểm Hot tối thiểu", "min_hot", "0–100", 90)
        self.rs_excl = entry(3, "Loại trừ từ (dấu phẩy)", "exclude", "vd: reaction, live", 200)
        self.rs_outl = ctk.CTkSwitch(vf, text="Chỉ video vượt trội\n(≥2× view thường của kênh)", font=self.font(12),
                                     text_color=T["TEXT"], progress_color=T["ACCENT"], command=self._rs_apply)
        (self.rs_outl.select if rcfg.get("outliers") else self.rs_outl.deselect)()
        self.rs_outl.grid(row=2, column=4, sticky="w", pady=(0, 12))

        # watched channels card
        ch = self.card(wrap)
        ch.grid_columnconfigure(0, weight=1)
        self.muted(ch, "Mỗi dòng một link kênh: YouTube, Twitch, Kick, TikTok, Instagram, X, Dailymotion, Bilibili, "
                       "Rumble... App lấy video mới nhất của từng kênh, so với view thường của kênh để tìm video vượt "
                       "trội, rồi xếp theo điểm Hot.", 12, wraplength=880).grid(row=0, column=0, columnspan=3, sticky="w",
                                                                        padx=14, pady=(12, 6))
        self.rs_sources = ctk.CTkTextbox(ch, height=110, fg_color=T["CARD2"], text_color=T["TEXT"], font=self.font(13),
                                         corner_radius=10)
        self.rs_sources.insert("1.0", "\n".join(self.cfg.get("watch_sources", [])))
        self.rs_sources.grid(row=1, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 8))
        perbox = ctk.CTkFrame(ch, fg_color="transparent")
        perbox.grid(row=2, column=0, sticky="w", padx=12, pady=(0, 6))
        self.rs_per = self._rs_filter(perbox, 0, "Video mỗi kênh", PER_CHANNEL, str(rcfg.get("per_channel", 10)))
        self.rs_scan = self.button(ch, "Quét kênh  →", self._rs_scan, primary=True, height=40, width=150)
        self.rs_scan.grid(row=2, column=2, sticky="e", padx=12, pady=(0, 12))
        self.rs_ch_card = ch

        # status
        self.rs_status = self.muted(wrap, "", 13, wraplength=900)
        self.rs_status.grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.rs_bar = ctk.CTkProgressBar(wrap, height=4, progress_color=T["ACCENT"], fg_color=T["CARD2"],
                                         mode="indeterminate")

        # results
        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.grid(row=7, column=0, sticky="ew", pady=(14, 6))
        head.grid_columnconfigure(0, weight=1)
        self.rs_head = self.h(head, "", 18)
        self.rs_head.grid(row=0, column=0, sticky="w")
        self.rs_hide_done = ctk.CTkSwitch(head, text="Ẩn video đã cắt", font=self.font(13), text_color=T["TEXT"],
                                          progress_color=T["ACCENT"], command=self._rs_render)
        self.rs_hide_done.grid(row=0, column=1, padx=10)
        self.button(head, "Chọn top 5", lambda: self._rs_select_top(5), height=32, width=100).grid(row=0, column=2)
        self.rs_list = ctk.CTkFrame(wrap, fg_color="transparent")
        self.rs_list.grid(row=8, column=0, sticky="ew")
        self.rs_list.grid_columnconfigure(0, weight=1)
        self._rs_lastcols = None
        self.rs_list.bind("<Configure>", lambda _e: self._rs_resized())
        act = ctk.CTkFrame(wrap, fg_color="transparent")
        act.grid(row=9, column=0, sticky="ew", pady=(12, 0))
        act.grid_columnconfigure(0, weight=1)
        self.rs_sel_lbl = self.muted(act, "", 13)
        self.rs_sel_lbl.grid(row=0, column=0, sticky="w")
        self.rs_queue_btn = self.button(act, "Thêm vào hàng chờ cắt clip", self._rs_queue_selected, primary=True,
                                        height=44, width=260)
        self.rs_queue_btn.grid(row=0, column=1)
        self.rs_items, self.rs_raw, self.rs_checks, self._rs_imgs, self.rs_page = [], [], {}, {}, 0
        self._rs_switch()
        self._rs_render()

    def _rs_filter(self, master, col, title, pairs, value, row=0):
        box = ctk.CTkFrame(master, fg_color="transparent")
        box.grid(row=row, column=col, sticky="w", padx=(0, 18), pady=(0, 8))
        self.muted(box, title, 11).grid(row=0, column=0, sticky="w")
        s = self.seg(box, pairs, value)
        s.configure(height=30, font=self.font(12))
        s.grid(row=1, column=0, sticky="w")
        return s

    def _rs_toggle_plat(self, key):
        T = self.theme
        if key:
            if key in self.rs_plats and len(self.rs_plats) > 1:
                self.rs_plats.discard(key)
            else:
                self.rs_plats.add(key)
        from . import platform_login
        locked = []
        for k, b in self.rs_chip.items():
            on = k in self.rs_plats
            site, required = research.LOGIN.get(k, (None, False))
            need = bool(site and required and not platform_login.is_logged_in(site))
            if need and on:
                locked.append(platform_login.SITES[site]["label"])
            b.configure(text=research.PLATFORMS[k]["label"] + (" 🔒" if need else " ✓" if site and required else ""),
                        fg_color=T["ACCENT"] if on else T["CARD2"], hover_color=T["ACCENT_HOVER"] if on else T["BORDER"],
                        text_color="#03161A" if on else T["TEXT"],
                        border_color=T["ACCENT"] if on else ("#FBBF24" if need else T["BORDER"]))
        hints = [research.PLATFORMS[k].get("hint") for k in research.PLATFORMS if k in self.rs_plats]
        text = "  ".join(dict.fromkeys(h for h in hints if h))
        if locked:
            text = (f"🔒 {', '.join(locked)} yêu cầu đăng nhập: bấm nút 🔒 tương ứng bên dưới để đăng nhập trước khi "
                    f"quét. ") + text
        self.rs_plat_hint.configure(text=text, text_color="#FBBF24" if locked else T["MUTED"])

    def _rs_filters(self):
        def num(e):
            t = e.get().strip().replace(".", "").replace(",", "").lower()
            mult = 1000 if t.endswith("k") else 1_000_000 if t.endswith("m") else 1
            t = t.rstrip("km")
            return int(float(t) * mult) if t.replace(".", "").isdigit() else 0
        return {"min_views": num(self.rs_minv), "min_vph": num(self.rs_minvph), "min_hot": num(self.rs_minhot),
                "exclude": self.rs_excl.get().strip(), "outliers": bool(self.rs_outl.get()),
                "order": _key(SORTS, self.rs_sort.get()),
                "period": _key(PERIODS, self.rs_period.get()) if self.rs_mode.get() == MODES[0] else None,
                "length": _key(LENGTHS, self.rs_len.get()) if self.rs_mode.get() == MODES[0] else None}

    def _rs_apply_later(self):
        if getattr(self, "_rs_after", None):
            self.after_cancel(self._rs_after)
        self._rs_after = self.after(400, self._rs_apply)

    def _rs_apply(self):
        self._rs_after = None
        self.rs_items = research.apply_filters(list(getattr(self, "rs_raw", [])), self._rs_filters())
        self._rs_render()
        try:
            self._rs_save_cfg()
        except Exception:
            pass

    def _rs_switch(self):
        kw = self.rs_mode.get() == MODES[0]
        show, hide = (self.rs_kw_card, self.rs_ch_card) if kw else (self.rs_ch_card, self.rs_kw_card)
        hide.grid_remove()
        show.grid(row=3, column=0, sticky="ew", pady=(12, 0))

    # ---------- run searches in the background ----------
    def _rs_busy(self, on, text=""):
        self.rs_status.configure(text=text, text_color=self.theme["MUTED"])
        for b in (self.rs_go, self.rs_scan):
            b.configure(state="disabled" if on else "normal")
        if on:
            self.rs_bar.grid(row=6, column=0, sticky="ew", pady=(6, 0))
            self.rs_bar.start()
        else:
            self.rs_bar.stop()
            self.rs_bar.grid_remove()

    def _researcher(self, log):
        t = tools.ensure(log)
        return research.Researcher(t["yt-dlp"], t["deno"], self.cfg.get("browser") or None, log,
                                   cookies=self.cfg.get("cookies_file") or None)

    def _rs_save_cfg(self):
        r = self.cfg.setdefault("research", {})
        f = self._rs_filters()
        r.update(period=_key(PERIODS, self.rs_period.get()), length=_key(LENGTHS, self.rs_len.get()),
                 order=f["order"], count=int(_key(COUNTS, self.rs_count.get())),
                 per_channel=int(_key(PER_CHANNEL, self.rs_per.get())), platforms=sorted(self.rs_plats),
                 min_views=f["min_views"] or "", min_vph=f["min_vph"] or "", min_hot=f["min_hot"] or "",
                 exclude=f["exclude"], outliers=f["outliers"])
        self.cfg["watch_sources"] = [l.strip() for l in self.rs_sources.get("1.0", "end").splitlines() if l.strip()]
        config.save(self.cfg)

    def _rs_search(self):
        q = self.rs_query.get().strip()
        if not q:
            self.rs_status.configure(text="Nhập từ khoá cần tìm.", text_color="#F87171")
            return
        self._rs_save_cfg()
        r = self.cfg["research"]
        keys = [k for k in research.PLATFORMS if k in self.rs_plats]
        queries = [x.strip() for x in q.split(",") if x.strip()][:5]
        names = ", ".join(research.PLATFORMS[k]["label"] for k in keys)
        self._rs_busy(True, f"Đang tìm “{q}” trên {names}...")
        yt_sort = "date" if r["order"] == "date" else "views"

        def work(rs):
            items, errors = rs.search_many(keys, queries, r["period"], r["length"], yt_sort, r["count"],
                                           progress=lambda m: self.q.put(("rs_status", m + "...")))
            self.q.put(("rs_status", f"Tìm thấy {len(items)} video, đang lấy ngày đăng và view thường của kênh "
                                     f"để chấm điểm Hot..."))
            return rs.enrich(items, top=15), errors
        self._rs_run(work, f"“{q}”")

    def _rs_scan(self):
        self._rs_save_cfg()
        urls = self.cfg["watch_sources"]
        if not urls:
            self.rs_status.configure(text="Thêm ít nhất một kênh (mỗi dòng một link).", text_color="#F87171")
            return
        self._rs_busy(True, f"Đang quét {len(urls)} kênh...")

        def work(rs):
            items, errors = rs.channels(urls, self.cfg["research"]["per_channel"])
            self.q.put(("rs_status", f"Lấy được {len(items)} video, đang chấm điểm Hot..."))
            return rs.enrich(items, top=15), errors
        self._rs_run(work, f"{len(urls)} kênh")

    def _rs_run(self, work, what):
        def thread():
            try:
                rs = self._researcher(lambda m: None)
                items, errors = work(rs)
                done = research.done_sources(self.cfg.get("output_dir") or config.default_output_dir())
                self.q.put(("rs_done", (research.score(items, done), errors, what)))
            except research.ResearchError as e:
                self.q.put(("rs_done", (None, [str(e)], what)))
            except Exception as e:
                self.q.put(("rs_done", (None, [f"Lỗi: {e}"], what)))
        threading.Thread(target=thread, daemon=True).start()

    def _rs_handle(self, kind, data):
        """Called from the main queue pump."""
        if kind == "rs_status":
            self.rs_status.configure(text=data)
        elif kind == "rs_done":
            items, errors, what = data
            self._rs_busy(False)
            if items is None:
                self.rs_status.configure(text=errors[0], text_color="#F87171")
                self.notify("Khám phá nguồn lỗi", errors[0][:120], ok=False)
                return
            self.rs_raw, self.rs_checks = items, {}
            self.rs_items = research.apply_filters(list(items), self._rs_filters())
            hidden = len(items) - len(self.rs_items)
            msg = (f"Xong: {len(items)} video cho {what}" + (f", bộ lọc viral ẩn {hidden}" if hidden else "") +
                   ". Điểm Hot = tốc độ tăng view, độ mới, độ dài, tương tác, mức vượt trội so với kênh.")
            if errors:
                msg += "\nKhông quét được: " + " | ".join(errors[:4])
            self.rs_status.configure(text=msg, text_color="#FBBF24" if errors else self.theme["MUTED"])
            self._rs_render()
            for x in self.rs_items[:3]:  # the likely first clicks are ready before you click
                self._prefetch(x["url"])
            self.notify("Khám phá nguồn xong", f"{len(self.rs_items)} video cho {what}"
                        + (f" · {len(errors)} nền tảng lỗi" if errors else ""), ok=not (errors and not items))
        elif kind == "rs_thumb_raw":
            key, im, item = data
            img = self._rs_card_image(item or {"url": key}, im)
            lbl = self._rs_thumb_labels.get(key)
            if lbl is not None and lbl.winfo_exists():
                lbl.configure(image=img)
        elif kind == "rs_thumb":
            key, img = data
            lbl = self._rs_thumb_labels.get(key)
            if lbl is not None and lbl.winfo_exists():
                self._rs_imgs[key] = img
                lbl.configure(image=img, text="")

    # ---------- results ----------
    # ---------- results: a grid of vertical cards, one page at a time ----------
    TW, TH = 232, 130  # thumbnail size on a card

    def _rs_cols(self):
        w = self.rs_list.winfo_width()
        if w < 50:
            w = self.winfo_width() - 330
        return max(3, min(6, w // 262))

    def _rs_resized(self):
        """Window resized: re-flow the cards when the number of columns changes."""
        if getattr(self, "_rs_rjob", None):
            self.after_cancel(self._rs_rjob)

        def go():
            self._rs_rjob = None
            if self.rs_items and self._rs_cols() != self._rs_lastcols:
                self._rs_render(keep_page=True)
        self._rs_rjob = self.after(250, go)

    def _rs_shown(self):
        return [x for x in self.rs_items if not (self.rs_hide_done.get() and "Đã cắt" in x.get("tags", []))]

    def _rs_go_page(self, d):
        self.rs_page = max(0, self.rs_page + d)
        self._rs_render(keep_page=True)
        try:  # back to the top of the results
            canvas = self.pages["research"]._parent_canvas
            canvas.update_idletasks()
            y = self.rs_head.winfo_rooty() - self.pages["research"].winfo_rooty()
            total = max(1, self.pages["research"].winfo_height())
            canvas.yview_moveto(max(0.0, y / total - 0.01))
        except Exception:
            pass

    def _rs_render(self, keep_page=False):
        T = self.theme
        for w in self.rs_list.winfo_children():
            w.destroy()
        self._rs_thumb_labels = {}
        items = self._rs_shown()
        self.rs_head.configure(text=f"Kết quả · {len(items)} video" if self.rs_items else "")
        if not keep_page:
            self.rs_page = 0
        if not items:
            self.muted(self.rs_list, "Kết quả sẽ hiện ở đây." if not self.rs_items else "Không còn video nào.", 14
                       ).grid(row=0, column=0, sticky="w", pady=16)
            self._rs_upd_sel()
            return
        cols = self._rs_cols()
        self._rs_lastcols = cols
        per = cols * 3
        pages = (len(items) + per - 1) // per
        self.rs_page = min(self.rs_page, pages - 1)
        for c in range(6):
            self.rs_list.grid_columnconfigure(c, weight=1 if c < cols else 0, uniform="rs" if c < cols else "")
        chunk = items[self.rs_page * per:(self.rs_page + 1) * per]
        bg = T["CARD"]
        for i, x in enumerate(chunk):
            card = ctk.CTkFrame(self.rs_list, fg_color=bg, corner_radius=14, border_width=1, border_color=T["BORDER"])
            card.grid(row=i // cols, column=i % cols, sticky="nsew", padx=5, pady=5)
            card.grid_columnconfigure(0, weight=1)
            card.bind("<Enter>", lambda _e, u=x["url"]: self._prefetch(u))
            th = ctk.CTkLabel(card, text="", image=self._rs_card_image(x), cursor="hand2")
            th.grid(row=0, column=0, padx=8, pady=(8, 4))
            th.bind("<Button-1>", lambda _e, x=x: self.open_player(x))
            self._rs_thumb_labels[x["url"]] = th
            if x["url"] not in self._rs_imgs and x.get("thumb"):
                self._rs_load_thumb(x["url"], x["thumb"], x)
            top = tk.Frame(card, bg=bg)
            top.grid(row=1, column=0, sticky="ew", padx=10)
            top.grid_columnconfigure(1, weight=1)
            var = self.rs_checks.get(x["url"]) or ctk.BooleanVar(value=False)
            self.rs_checks[x["url"]] = var
            ctk.CTkCheckBox(top, text="", variable=var, width=22, checkbox_width=20, checkbox_height=20,
                            fg_color=T["ACCENT"], hover_color=T["ACCENT_HOVER"], bg_color=bg,
                            command=self._rs_upd_sel).grid(row=0, column=0, sticky="nw", pady=(2, 0))
            title = x["title"] if len(x["title"]) <= 90 else x["title"][:88].rstrip() + "…"
            tk.Label(top, text=title, font=self._tkfont(12, "bold"), fg=T["TEXT"], bg=bg, anchor="nw", justify="left",
                     wraplength=self.TW - 30, height=2).grid(row=0, column=1, sticky="nw")
            vph = f"{research.human_views(x['velocity'])}/giờ" if x.get("velocity") else ""
            meta = " · ".join(v for v in (f"{research.human_views(x['views'])} view", vph,
                                          research.human_age(x.get("age_h"))) if v)
            tk.Label(card, text=(x.get("channel") or "")[:34], font=self._tkfont(10), fg=T["MUTED"], bg=bg,
                     anchor="w").grid(row=2, column=0, sticky="ew", padx=12)
            tk.Label(card, text=meta, font=self._tkfont(10), fg=T["MUTED"], bg=bg, anchor="w").grid(
                row=3, column=0, sticky="ew", padx=12)
            tags = [t for t in x.get("tags", []) if t != "Dài"]
            tk.Label(card, text="  ·  ".join(tags) if tags else " ", font=self._tkfont(10, "bold"),
                     fg=TAG_COLORS.get(tags[0].split(" x")[0], T["MUTED"]) if tags else T["MUTED"], bg=bg,
                     anchor="w").grid(row=4, column=0, sticky="ew", padx=12)
            btns = tk.Frame(card, bg=bg)
            btns.grid(row=5, column=0, sticky="ew", padx=8, pady=(4, 10))
            btns.grid_columnconfigure((0, 1, 2), weight=1, uniform="b")
            ctk.CTkButton(btns, text="▶ Xem", height=28, width=10, corner_radius=8, fg_color=T["CARD2"],
                          hover_color=T["BORDER"], text_color=T["TEXT"], font=self.font(12), bg_color=bg,
                          command=lambda x=x: self.open_player(x)).grid(row=0, column=0, sticky="ew", padx=2)
            sv = ctk.CTkButton(btns, text="🔖 Lưu", height=28, width=10, corner_radius=8, fg_color=T["CARD2"],
                               hover_color=T["BORDER"], text_color=T["TEXT"], font=self.font(12), bg_color=bg)
            sv.configure(command=lambda x=x, sv=sv: self.save_link_menu(x, sv))
            sv.grid(row=0, column=1, sticky="ew", padx=2)
            ctk.CTkButton(btns, text="Cắt", height=28, width=10, corner_radius=8, fg_color=T["ACCENT"],
                          hover_color=T["ACCENT_HOVER"], text_color="#03161A", font=self.font(12, "bold"),
                          bg_color=bg, command=lambda u=x["url"]: self.enqueue([u], front=True)).grid(
                row=0, column=2, sticky="ew", padx=2)
        if pages > 1:
            pg = tk.Frame(self.rs_list, bg=T["BG"])
            pg.grid(row=99, column=0, columnspan=cols, pady=(10, 0))
            self.button(pg, "‹ Trang trước", lambda: self._rs_go_page(-1), height=34, width=120,
                        state="normal" if self.rs_page > 0 else "disabled").grid(row=0, column=0, padx=6)
            tk.Label(pg, text=f"Trang {self.rs_page + 1}/{pages}", font=self._tkfont(12, "bold"), fg=T["TEXT"],
                     bg=T["BG"]).grid(row=0, column=1, padx=10)
            self.button(pg, "Trang sau ›", lambda: self._rs_go_page(1), height=34, width=120,
                        state="normal" if self.rs_page < pages - 1 else "disabled").grid(row=0, column=2, padx=6)
            self.rs_head.configure(text=f"Kết quả · {len(items)} video · trang {self.rs_page + 1}/{pages}")
        self._rs_upd_sel()

    def _tkfont(self, size, weight="normal"):
        """Plain Tk font (cards use light Tk labels: far faster to draw and scroll than canvas widgets)."""
        key = (size, weight)
        cache = self.__dict__.setdefault("_tkfonts", {})
        if key not in cache:
            import tkinter.font as tkfont
            fam = getattr(self, "F", None) or "Segoe UI"
            cache[key] = tkfont.Font(family=fam, size=-int(size * 1.34), weight=weight)
        return cache[key]

    def _rs_card_image(self, x, im=None):
        """Thumbnail with the Hot score, platform and length drawn on it (one image instead of 3 widgets)."""
        key = x["url"]
        if im is None and key in self._rs_imgs:
            return self._rs_imgs[key]
        from PIL import Image, ImageDraw, ImageFont
        W2, H2 = self.TW * 2, self.TH * 2
        base = im.copy() if im is not None else Image.new("RGB", (W2, H2), (38, 40, 48))
        d = ImageDraw.Draw(base, "RGBA")
        fd = os.environ.get("AUTOCLIPPER_FONTS") or config.resource_path("assets", "fonts")
        try:
            f_big = ImageFont.truetype(os.path.join(fd, "Inter-ExtraBold.otf"), 34)
            f_small = ImageFont.truetype(os.path.join(fd, "Inter-ExtraBold.otf"), 22)
        except Exception:
            f_big = f_small = ImageFont.load_default()
        hot = str(x.get("hot", ""))
        hot_bg = (34, 211, 238, 255) if x.get("hot", 0) >= 70 else (15, 16, 20, 215)
        d.rounded_rectangle([10, 10, 10 + 26 + 22 * len(hot), 58], radius=12, fill=hot_bg)
        d.text((23, 14), hot, font=f_big, fill=(3, 22, 26) if x.get("hot", 0) >= 70 else (240, 240, 240))
        plat = x.get("platform", "")
        col = PLAT_COLORS.get(plat, "#334155").lstrip("#")
        rgb = tuple(int(col[i:i + 2], 16) for i in (0, 2, 4))
        pw = int(d.textlength(plat, font=f_small)) + 20
        d.rounded_rectangle([10, H2 - 44, 10 + pw, H2 - 10], radius=8, fill=rgb + (235,))
        d.text((20, H2 - 41), plat, font=f_small, fill=(255, 255, 255))
        dur = research.human_dur(x.get("duration"))
        if dur:
            dw = int(d.textlength(dur, font=f_small)) + 18
            d.rounded_rectangle([W2 - 10 - dw, H2 - 44, W2 - 10, H2 - 10], radius=8, fill=(0, 0, 0, 200))
            d.text((W2 - 10 - dw + 9, H2 - 41), dur, font=f_small, fill=(255, 255, 255))
        img = ctk.CTkImage(base, base, size=(self.TW, self.TH))
        if im is not None:
            self._rs_imgs[key] = img
        return img

    def _prefetch(self, url):
        from . import player
        player.prefetch(url, self.cfg)

    def _rs_load_thumb(self, key, url, item=None):
        def work():
            try:
                import httpx
                from PIL import Image, ImageOps
                r = httpx.get(url, timeout=15, follow_redirects=True)
                im = ImageOps.fit(Image.open(io.BytesIO(r.content)).convert("RGB"), (self.TW * 2, self.TH * 2))
                self.q.put(("rs_thumb_raw", (key, im, item)))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _rs_selected(self):
        return [x["url"] for x in self.rs_items if self.rs_checks.get(x["url"]) and self.rs_checks[x["url"]].get()]

    def _rs_upd_sel(self):
        n = len(self._rs_selected())
        self.rs_sel_lbl.configure(text=f"Đã chọn {n} video. Chúng sẽ được tải và cắt lần lượt với cài đặt ở trang "
                                       f"Tạo clip." if n else "Tick vào video muốn cắt, hoặc bấm 'Cắt ngay'.")
        self.rs_queue_btn.configure(state="normal" if n else "disabled",
                                    text=f"Thêm {n} video vào hàng chờ" if n else "Thêm vào hàng chờ cắt clip")

    def _rs_select_top(self, n):
        shown = [x for x in self.rs_items if not (self.rs_hide_done.get() and "Đã cắt" in x.get("tags", []))]
        for x in shown[:n]:
            self.rs_checks.setdefault(x["url"], ctk.BooleanVar()).set(True)
        self._rs_upd_sel()

    def _rs_queue_selected(self):
        urls = self._rs_selected()
        if urls:
            self.enqueue(urls)
            for u in urls:
                self.rs_checks[u].set(False)
            self._rs_upd_sel()
