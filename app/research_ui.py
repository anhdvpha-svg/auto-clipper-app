"""'Khám phá nguồn' page: find videos worth clipping, then queue them into the clipper."""
import io, os, threading, webbrowser

import customtkinter as ctk

from . import config, research, tools

MODES = ["Tìm theo từ khoá", "Kênh theo dõi"]
PERIODS = [("today", "Hôm nay"), ("week", "Tuần này"), ("month", "Tháng này"), ("year", "Năm nay"), ("any", "Mọi lúc")]
LENGTHS = [("long", "Dài >20 phút"), ("medium", "4–20 phút"), ("any", "Mọi độ dài")]
SORTS = [("views", "Lượt xem"), ("date", "Mới nhất"), ("relevance", "Liên quan")]
COUNTS = [("20", "20"), ("40", "40")]
PER_CHANNEL = [("5", "5"), ("10", "10"), ("20", "20")]
TAG_COLORS = {"Đang lên": "#F97316", "Mới": "#22C55E", "Dài": "#64748B", "Đang live": "#EF4444", "Đã cắt": "#6366F1"}


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
                                     placeholder_text="Từ khoá: tên project, creator, chủ đề trend... (vd: bitcoin podcast)")
        self.rs_query.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=(12, 8))
        self.rs_query.bind("<Return>", lambda _: self._rs_search())
        self.rs_go = self.button(kw, "Tìm  →", self._rs_search, primary=True, height=48, width=120)
        self.rs_go.grid(row=0, column=1, padx=(0, 12), pady=(12, 8))
        filt = ctk.CTkFrame(kw, fg_color="transparent")
        filt.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        rcfg = self.cfg.setdefault("research", {})
        self.rs_period = self._rs_filter(filt, 0, "Đăng trong", PERIODS, rcfg.get("period", "week"))
        self.rs_len = self._rs_filter(filt, 1, "Độ dài", LENGTHS, rcfg.get("length", "long"))
        self.rs_sort = self._rs_filter(filt, 0, "Sắp xếp", SORTS, rcfg.get("sort", "views"), row=1)
        self.rs_count = self._rs_filter(filt, 1, "Số kết quả", COUNTS, str(rcfg.get("count", 20)), row=1)
        self.rs_kw_card = kw

        # watched channels card
        ch = self.card(wrap)
        ch.grid_columnconfigure(0, weight=1)
        self.muted(ch, "Mỗi dòng một kênh: YouTube (@kenh), Twitch, Kick hoặc TikTok (@user). App lấy video mới nhất "
                       "của từng kênh rồi xếp theo điểm Hot.", 12, wraplength=880).grid(row=0, column=0, columnspan=3, sticky="w",
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
        self.rs_status.grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.rs_bar = ctk.CTkProgressBar(wrap, height=4, progress_color=T["ACCENT"], fg_color=T["CARD2"],
                                         mode="indeterminate")

        # results
        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.grid(row=6, column=0, sticky="ew", pady=(14, 6))
        head.grid_columnconfigure(0, weight=1)
        self.rs_head = self.h(head, "", 18)
        self.rs_head.grid(row=0, column=0, sticky="w")
        self.rs_hide_done = ctk.CTkSwitch(head, text="Ẩn video đã cắt", font=self.font(13), text_color=T["TEXT"],
                                          progress_color=T["ACCENT"], command=self._rs_render)
        self.rs_hide_done.grid(row=0, column=1, padx=10)
        self.button(head, "Chọn top 5", lambda: self._rs_select_top(5), height=32, width=100).grid(row=0, column=2)
        self.rs_list = ctk.CTkFrame(wrap, fg_color="transparent")
        self.rs_list.grid(row=7, column=0, sticky="ew")
        self.rs_list.grid_columnconfigure(0, weight=1)
        act = ctk.CTkFrame(wrap, fg_color="transparent")
        act.grid(row=8, column=0, sticky="ew", pady=(12, 0))
        act.grid_columnconfigure(0, weight=1)
        self.rs_sel_lbl = self.muted(act, "", 13)
        self.rs_sel_lbl.grid(row=0, column=0, sticky="w")
        self.rs_queue_btn = self.button(act, "Thêm vào hàng chờ cắt clip", self._rs_queue_selected, primary=True,
                                        height=44, width=260)
        self.rs_queue_btn.grid(row=0, column=1)
        self.rs_items, self.rs_checks, self._rs_imgs = [], {}, {}
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
            self.rs_bar.grid(row=5, column=0, sticky="ew", pady=(6, 0))
            self.rs_bar.start()
        else:
            self.rs_bar.stop()
            self.rs_bar.grid_remove()

    def _researcher(self, log):
        t = tools.ensure(log)
        return research.Researcher(t["yt-dlp"], t["deno"], self.cfg.get("browser") or None, log)

    def _rs_save_cfg(self):
        r = self.cfg.setdefault("research", {})
        r.update(period=_key(PERIODS, self.rs_period.get()), length=_key(LENGTHS, self.rs_len.get()),
                 sort=_key(SORTS, self.rs_sort.get()), count=int(_key(COUNTS, self.rs_count.get())),
                 per_channel=int(_key(PER_CHANNEL, self.rs_per.get())))
        self.cfg["watch_sources"] = [l.strip() for l in self.rs_sources.get("1.0", "end").splitlines() if l.strip()]
        config.save(self.cfg)

    def _rs_search(self):
        q = self.rs_query.get().strip()
        if not q:
            self.rs_status.configure(text="Nhập từ khoá cần tìm.", text_color="#F87171")
            return
        self._rs_save_cfg()
        r = self.cfg["research"]
        self._rs_busy(True, f"Đang tìm “{q}” trên YouTube...")

        def work(rs):
            items = rs.search(q, r["period"], r["length"], r["sort"], r["count"])
            self.q.put(("rs_status", f"Tìm thấy {len(items)} video, đang lấy ngày đăng để chấm điểm Hot..."))
            return rs.enrich(items, top=12), []
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
                return
            self.rs_items, self.rs_checks = items, {}
            msg = f"Xong: {len(items)} video cho {what}, sắp theo điểm Hot (tốc độ tăng view, độ mới, độ dài, tương tác)."
            if errors:
                msg += "\nKhông quét được: " + " | ".join(errors)
            self.rs_status.configure(text=msg, text_color="#FBBF24" if errors else self.theme["MUTED"])
            self._rs_render()
        elif kind == "rs_thumb":
            key, img = data
            lbl = self._rs_thumb_labels.get(key)
            if lbl is not None and lbl.winfo_exists():
                self._rs_imgs[key] = img
                lbl.configure(image=img, text="")

    # ---------- results ----------
    def _rs_render(self):
        T = self.theme
        for w in self.rs_list.winfo_children():
            w.destroy()
        self._rs_thumb_labels = {}
        items = [x for x in self.rs_items if not (self.rs_hide_done.get() and "Đã cắt" in x.get("tags", []))]
        self.rs_head.configure(text=f"Kết quả · {len(items)} video" if self.rs_items else "")
        if not items:
            self.muted(self.rs_list, "Kết quả sẽ hiện ở đây." if not self.rs_items else "Không còn video nào.", 14
                       ).grid(row=0, column=0, sticky="w", pady=16)
            self._rs_upd_sel()
            return
        for i, x in enumerate(items[:80]):
            row = ctk.CTkFrame(self.rs_list, fg_color=T["CARD"], corner_radius=14, border_width=1,
                               border_color=T["BORDER"])
            row.grid(row=i, column=0, sticky="ew", pady=4)
            row.grid_columnconfigure(3, weight=1)
            var = self.rs_checks.get(x["url"]) or ctk.BooleanVar(value=False)
            self.rs_checks[x["url"]] = var
            ctk.CTkCheckBox(row, text="", variable=var, width=24, fg_color=T["ACCENT"], hover_color=T["ACCENT_HOVER"],
                            command=self._rs_upd_sel).grid(row=0, column=0, rowspan=3, padx=(14, 6))
            hot = ctk.CTkLabel(row, text=str(x["hot"]), width=46, height=46, corner_radius=12,
                               fg_color=T["ACCENT"] if x["hot"] >= 70 else T["CARD2"],
                               text_color="#03161A" if x["hot"] >= 70 else T["TEXT"], font=self.font(17, "bold"))
            hot.grid(row=0, column=1, rowspan=3, padx=6)
            th = ctk.CTkLabel(row, text="", width=160, height=90, fg_color=T["CARD2"], corner_radius=8)
            th.grid(row=0, column=2, rowspan=3, padx=8, pady=10)
            self._rs_thumb_labels[x["url"]] = th
            if x["url"] in self._rs_imgs:
                th.configure(image=self._rs_imgs[x["url"]])
            elif x.get("thumb"):
                self._rs_load_thumb(x["url"], x["thumb"])
            ctk.CTkLabel(row, text=x["title"], font=self.font(14, "bold"), text_color=T["TEXT"], anchor="w",
                         justify="left", wraplength=560).grid(row=0, column=3, sticky="sw", pady=(10, 0))
            meta = " · ".join(v for v in (x["platform"], x["channel"], f"{research.human_views(x['views'])} view",
                                          research.human_dur(x["duration"]), research.human_age(x.get("age_h"))) if v)
            self.muted(row, meta, 12).grid(row=1, column=3, sticky="w")
            tags = ctk.CTkFrame(row, fg_color="transparent")
            tags.grid(row=2, column=3, sticky="nw", pady=(2, 10))
            for j, t in enumerate(x.get("tags", [])):
                ctk.CTkLabel(tags, text=t, font=self.font(11, "bold"), text_color="white", corner_radius=6, height=20,
                             fg_color=TAG_COLORS.get(t, T["CARD2"])).grid(row=0, column=j, padx=(0, 5), ipadx=6)
            btns = ctk.CTkFrame(row, fg_color="transparent")
            btns.grid(row=0, column=4, rowspan=3, padx=(6, 14))
            self.button(btns, "Xem", lambda u=x["url"]: webbrowser.open(u), width=70, height=32).grid(row=0, column=0,
                                                                                                     pady=3)
            self.button(btns, "Cắt ngay", lambda u=x["url"]: self.enqueue([u], front=True), primary=True, width=90,
                        height=32).grid(row=1, column=0, pady=3)
        self._rs_upd_sel()

    def _rs_load_thumb(self, key, url):
        def work():
            try:
                import httpx
                from PIL import Image, ImageOps
                r = httpx.get(url, timeout=15, follow_redirects=True)
                im = ImageOps.fit(Image.open(io.BytesIO(r.content)).convert("RGB"), (320, 180))
                self.q.put(("rs_thumb", (key, ctk.CTkImage(im, im, size=(160, 90)))))
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
