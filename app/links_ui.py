"""'Kho link' page (saved source links per project), the 'Lưu link' menu, the in-app player and platform logins."""
import io, threading, time, tkinter as tk, webbrowser

import customtkinter as ctk

from . import links, platform_login, research

PLAT_COLORS = {"YouTube": "#EF4444", "YouTube Shorts": "#F43F5E", "TikTok": "#111827", "Instagram": "#C026D3",
               "Twitch": "#9146FF", "Kick": "#53FC18", "Dailymotion": "#0066DC", "Bilibili": "#00A1D6",
               "X": "#000000", "Rumble": "#85C742", "Facebook": "#1877F2"}


class LinksMixin:
    # ---------------- shared helpers ----------------
    def toast(self, text, color=None):
        T = self.theme
        lbl = ctk.CTkLabel(self, text=text, font=self.font(13, "bold"), text_color="#03161A",
                           fg_color=color or T["ACCENT"], corner_radius=10, height=36)
        lbl.place(relx=0.5, rely=0.97, anchor="s")
        lbl.lift()
        self.after(2600, lbl.destroy)

    def notify(self, title, body="", ok=True):
        """Every finished task: toast in the app; when the app is not in front also a Windows notification,
        a flashing taskbar button and a sound."""
        from . import notify
        try:
            self.toast(f"{'✓' if ok else '⚠'} {title}" + (f" · {body}" if body else ""),
                       None if ok else "#FBBF24")
        except Exception:
            pass
        if not notify.app_has_focus(self):
            notify.system_toast(title, body)
            notify.flash_and_beep(self, ok)

    def open_player(self, item):
        from .player import PlayerWindow
        PlayerWindow(self, item)

    def save_link_menu(self, item, widget=None):
        """Popup: pick the project to save this link into (or create one)."""
        m = tk.Menu(self, tearoff=0, bg="#1F2128", fg="#E7E9EE", activebackground="#22D3EE",
                    activeforeground="#03161A", font=("Segoe UI", 11))
        d = links.load()
        names = [p["name"] for p in d["projects"]]
        if d.get("last") in names:  # most recently used first
            names.remove(d["last"])
            names.insert(0, d["last"])
        m.add_command(label="Lưu vào dự án:", state="disabled")
        for n in names[:15]:
            m.add_command(label=f"   {n}", command=lambda n=n: self._save_link(n, item))
        if names:
            m.add_separator()
        m.add_command(label="➕  Dự án mới...", command=lambda: self._save_link(None, item))
        w = widget or self
        try:
            x, y = w.winfo_rootx(), w.winfo_rooty() + w.winfo_height()
        except Exception:
            x, y = self.winfo_pointerx(), self.winfo_pointery()
        m.tk_popup(x, y)

    def _save_link(self, name, item):
        if not name:
            dlg = ctk.CTkInputDialog(title="Dự án mới", text="Tên dự án / campaign (vd: Campaign Bitcoin tháng 11):")
            name = (dlg.get_input() or "").strip()
            if not name:
                return
        new = links.add(name, item)
        self.toast(f"Đã lưu vào “{name}”" if new else f"Link đã có trong “{name}”")
        self._lk_current = name
        if getattr(self, "lk_list", None) is not None and self.lk_list.winfo_ismapped():
            self._lk_refresh()

    # ---------------- platform login ----------------
    def login_site(self, site, after=None):
        """Open the in-app login window; when it closes, refresh every login button (and run `after` if logged in)."""
        try:
            proc = platform_login.start_login(site)
        except Exception as e:
            self.toast(f"Không mở được cửa sổ đăng nhập: {e}", "#F87171")
            return
        self.toast(f"Đăng nhập {platform_login.SITES[site]['label']} trong cửa sổ vừa mở...")

        def wait():
            if proc.poll() is None:
                self.after(800, wait)
                return
            ok = platform_login.is_logged_in(site)
            self.notify(f"Đã đăng nhập {platform_login.SITES[site]['label']}" if ok
                        else f"Chưa đăng nhập {platform_login.SITES[site]['label']}", "", ok)
            self.refresh_logins()
            if ok and after:
                after()
        self.after(1500, wait)

    def refresh_logins(self):
        for fn in getattr(self, "_login_refreshers", []):
            try:
                fn()
            except Exception:
                pass

    def login_buttons(self, master, sites, compact=False):
        """A row of '🔒 TikTok' / '✓ TikTok' buttons; click to log in (or, when logged in, to log in again/out)."""
        T = self.theme
        box = ctk.CTkFrame(master, fg_color="transparent")
        btns = {}

        def paint():
            for s, b in btns.items():
                ok = platform_login.is_logged_in(s)
                lab = platform_login.SITES[s]["label"]
                b.configure(text=f"✓ {lab}" if ok else f"🔒 Đăng nhập {lab}" if not compact else f"🔒 {lab}",
                            fg_color="#14532D" if ok else T["CARD2"], hover_color="#166534" if ok else T["BORDER"],
                            text_color="#BBF7D0" if ok else T["TEXT"], border_color="#22C55E" if ok else "#FBBF24")

        def click(s, b):
            if not platform_login.is_logged_in(s):
                self.login_site(s)
                return
            m = tk.Menu(self, tearoff=0, bg="#1F2128", fg="#E7E9EE", activebackground="#22D3EE",
                        activeforeground="#03161A", font=("Segoe UI", 11))
            m.add_command(label="Đăng nhập lại / đổi tài khoản", command=lambda: self.login_site(s))
            m.add_command(label="Đăng xuất khỏi app", command=lambda: (platform_login.logout(s), self.refresh_logins()))
            m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        for i, s in enumerate(sites):
            b = ctk.CTkButton(box, text="", height=30, corner_radius=15, font=self.font(12, "bold"), border_width=1)
            b.configure(command=lambda s=s, b=b: click(s, b))
            b.grid(row=0, column=i, padx=(0, 6), pady=2)
            btns[s] = b
        paint()
        self._login_refreshers = getattr(self, "_login_refreshers", []) + [paint]
        return box

    # ---------------- Kho link page ----------------
    def _build_links(self):
        T = self.theme
        page = ctk.CTkScrollableFrame(self, fg_color=T["BG"], corner_radius=0)
        page.grid_columnconfigure(0, weight=1)
        self.pages["links"] = page
        wrap = ctk.CTkFrame(page, fg_color="transparent")
        wrap.grid(row=0, column=0, sticky="ew", padx=36, pady=(30, 30))
        wrap.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(wrap, text="Kho link", font=self.font(28, "bold"), text_color=T["TEXT"], anchor="w").grid(
            row=0, column=0, sticky="w")
        self.muted(wrap, "Lưu link nguồn theo từng dự án / campaign. Bấm 🔖 Lưu ở trang Khám phá nguồn hoặc trong trình "
                         "xem video, hoặc dán link vào ô bên dưới.", 14, wraplength=900).grid(row=1, column=0, sticky="w",
                                                                                        pady=(4, 14))
        bar = self.card(wrap)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_columnconfigure(5, weight=1)
        self.muted(bar, "Dự án", 13).grid(row=0, column=0, padx=(16, 8), pady=14)
        self.lk_proj = ctk.CTkOptionMenu(bar, values=["(chưa có dự án)"], width=260, height=34, corner_radius=10,
                                         fg_color=T["CARD2"], button_color=T["CARD2"], button_hover_color=T["BORDER"],
                                         dropdown_fg_color=T["CARD2"], text_color=T["TEXT"], font=self.font(13),
                                         command=lambda v: self._lk_pick(v))
        self.lk_proj.grid(row=0, column=1, pady=14)
        self.button(bar, "➕ Dự án mới", self._lk_new, width=110, height=34).grid(row=0, column=2, padx=(8, 4))
        self.button(bar, "Đổi tên", self._lk_rename, width=80, height=34).grid(row=0, column=3, padx=4)
        self.lk_del = self.button(bar, "Xoá dự án", self._lk_delete, width=90, height=34)
        self.lk_del.grid(row=0, column=4, padx=4)
        self.lk_url = ctk.CTkEntry(bar, height=40, corner_radius=10, border_width=0, fg_color=T["CARD2"],
                                   font=self.font(14), text_color=T["TEXT"], placeholder_text_color=T["MUTED"],
                                   placeholder_text="Dán link video để lưu vào dự án này (nhiều link: mỗi dòng/cách "
                                                    "nhau bằng dấu cách)")
        self.lk_url.grid(row=1, column=0, columnspan=6, sticky="ew", padx=(16, 8), pady=(0, 14))
        self.lk_url.bind("<Return>", lambda _e: self._lk_add_typed())
        self.button(bar, "Lưu link", self._lk_add_typed, primary=True, width=110, height=40).grid(
            row=1, column=6, padx=(0, 16), pady=(0, 14))
        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.grid(row=3, column=0, sticky="ew", pady=(16, 6))
        head.grid_columnconfigure(0, weight=1)
        self.lk_head = self.h(head, "", 18)
        self.lk_head.grid(row=0, column=0, sticky="w")
        self.button(head, "Copy tất cả link", self._lk_copy_all, width=130, height=32).grid(row=0, column=1, padx=6)
        self.button(head, "Cắt tất cả  →", self._lk_cut_all, primary=True, width=130, height=32).grid(row=0, column=2)
        self.lk_list = ctk.CTkFrame(wrap, fg_color="transparent")
        self.lk_list.grid(row=4, column=0, sticky="ew")
        self.lk_list.grid_columnconfigure(0, weight=1)
        self._lk_current, self._lk_imgs, self._lk_labels = None, {}, {}

    def _lk_names(self):
        return links.names()

    def _lk_refresh(self):
        names = self._lk_names()
        d = links.load()
        if self._lk_current not in names:
            self._lk_current = d.get("last") if d.get("last") in names else (names[0] if names else None)
        self.lk_proj.configure(values=names or ["(chưa có dự án)"])
        self.lk_proj.set(self._lk_current or "(chưa có dự án)")
        self._lk_render()

    def _lk_pick(self, v):
        if v in self._lk_names():
            self._lk_current = v
            self._lk_render()

    def _lk_new(self):
        dlg = ctk.CTkInputDialog(title="Dự án mới", text="Tên dự án / campaign:")
        name = (dlg.get_input() or "").strip()
        if name:
            d = links.load()
            links.project(d, name)
            d["last"] = name
            links.save(d)
            self._lk_current = name
            self._lk_refresh()

    def _lk_rename(self):
        if not self._lk_current:
            return
        dlg = ctk.CTkInputDialog(title="Đổi tên dự án", text=f"Tên mới cho “{self._lk_current}”:")
        new = (dlg.get_input() or "").strip()
        if new and links.rename(self._lk_current, new):
            self._lk_current = new
            self._lk_refresh()

    def _lk_delete(self):
        if not self._lk_current:
            return
        if getattr(self, "_lk_confirm", None) != self._lk_current:  # two clicks to delete
            self._lk_confirm = self._lk_current
            self.lk_del.configure(text="Bấm lần nữa", fg_color="#7F1D1D")
            self.after(3000, lambda: (setattr(self, "_lk_confirm", None),
                                      self.lk_del.configure(text="Xoá dự án", fg_color=self.theme["CARD2"])))
            return
        links.delete(self._lk_current)
        self._lk_current, self._lk_confirm = None, None
        self.lk_del.configure(text="Xoá dự án", fg_color=self.theme["CARD2"])
        self._lk_refresh()

    def _lk_add_typed(self):
        import re
        urls = re.findall(r"https?://\S+", self.lk_url.get())
        if not urls:
            self.toast("Dán link bắt đầu bằng http...", "#FBBF24")
            return
        name = self._lk_current
        if not name:
            dlg = ctk.CTkInputDialog(title="Dự án mới", text="Chưa có dự án. Tên dự án / campaign:")
            name = (dlg.get_input() or "").strip()
            if not name:
                return
            self._lk_current = name
        for u in urls:
            links.add(name, {"url": u, "title": u, "platform": research.platform_of(u)})
        self.lk_url.delete(0, "end")
        self._lk_refresh()
        threading.Thread(target=self._lk_fetch_meta, args=(name, urls), daemon=True).start()

    def _lk_fetch_meta(self, name, urls):
        """Title, thumbnail, views for links typed in by hand."""
        try:
            rs = self._researcher(lambda m: None)
        except Exception:
            return
        for u in urls:
            try:
                d = rs._json(["-J", "--skip-download", "--no-playlist", u], timeout=90)
            except Exception:
                continue
            links.update(name, u, title=d.get("title"), channel=d.get("uploader") or d.get("channel"),
                         views=d.get("view_count"), duration=d.get("duration"), thumb=d.get("thumbnail"),
                         ts=d.get("timestamp"))
        self.q.put(("lk_refresh", len(urls)))

    def _lk_items(self):
        p = links.project(links.load(), self._lk_current, create=False) if self._lk_current else None
        return (p or {}).get("items", [])

    def _lk_render(self):
        T = self.theme
        for w in self.lk_list.winfo_children():
            w.destroy()
        self._lk_labels = {}
        items = self._lk_items()
        self.lk_head.configure(text=f"{self._lk_current} · {len(items)} link" if self._lk_current else "")
        if not items:
            self.muted(self.lk_list, "Chưa có link nào. Bấm 🔖 Lưu ở trang Khám phá nguồn hoặc dán link ở trên." if
                       self._lk_current else "Tạo dự án đầu tiên bằng nút ➕ Dự án mới.", 14).grid(
                row=0, column=0, sticky="w", pady=16)
            return
        for i, x in enumerate(items):
            row = ctk.CTkFrame(self.lk_list, fg_color=T["CARD"], corner_radius=14, border_width=1,
                               border_color=T["BORDER"])
            row.grid(row=i, column=0, sticky="ew", pady=4)
            row.grid_columnconfigure(1, weight=1)
            row.bind("<Enter>", lambda _e, u=x["url"]: self._prefetch(u))
            th = ctk.CTkLabel(row, text="", width=144, height=81, fg_color=T["CARD2"], corner_radius=8,
                              cursor="hand2")
            th.grid(row=0, column=0, rowspan=3, padx=12, pady=10)
            th.bind("<Button-1>", lambda _e, x=x: self.open_player(x))
            self._lk_labels[x["url"]] = th
            if x["url"] in self._lk_imgs:
                th.configure(image=self._lk_imgs[x["url"]])
            elif x.get("thumb"):
                self._lk_load_thumb(x["url"], x["thumb"])
            ctk.CTkLabel(row, text=x.get("title") or x["url"], font=self.font(14, "bold"), text_color=T["TEXT"],
                         anchor="w", justify="left", wraplength=620).grid(row=0, column=1, sticky="sw", pady=(10, 0))
            m = ctk.CTkFrame(row, fg_color="transparent")
            m.grid(row=1, column=1, sticky="w")
            plat = x.get("platform") or research.platform_of(x["url"])
            ctk.CTkLabel(m, text=plat, font=self.font(10, "bold"), text_color="white", height=18, corner_radius=5,
                         fg_color=PLAT_COLORS.get(plat, T["CARD2"])).grid(row=0, column=0, ipadx=5, padx=(0, 6))
            meta = " · ".join(v for v in (x.get("channel") or "",
                                          f"{research.human_views(x['views'])} view" if x.get("views") else "",
                                          research.human_dur(x.get("duration")),
                                          "lưu " + time.strftime("%d/%m %H:%M", time.localtime(x.get("saved", 0))))
                              if v)
            self.muted(m, meta, 12).grid(row=0, column=1, sticky="w")
            if x.get("caption"):
                self.muted(row, x["caption"].replace("\n", " ")[:140], 12, wraplength=620).grid(
                    row=2, column=1, sticky="nw", pady=(2, 10))
            b = ctk.CTkFrame(row, fg_color="transparent")
            b.grid(row=0, column=2, rowspan=3, padx=(6, 14))
            self.button(b, "▶ Xem", lambda x=x: self.open_player(x), width=84, height=30).grid(row=0, column=0, padx=3,
                                                                                            pady=3)
            self.button(b, "Copy link", lambda u=x["url"]: self._lk_copy(u, "link"), width=84, height=30).grid(
                row=0, column=1, padx=3, pady=3)
            self.button(b, "Xoá", lambda u=x["url"]: (links.remove(self._lk_current, u), self._lk_render()),
                        width=60, height=30).grid(row=0, column=2, padx=3, pady=3)
            if x.get("caption"):
                self.button(b, "Copy caption", lambda c=x["caption"]: self._lk_copy(c, "caption"), width=110,
                            height=30).grid(row=1, column=0, columnspan=2, sticky="ew", padx=3, pady=3)
            self.button(b, "Cắt ngay", lambda u=x["url"]: self.enqueue([u], front=True), primary=True, width=90,
                        height=30).grid(row=1, column=2 if x.get("caption") else 0,
                                        columnspan=1 if x.get("caption") else 3, sticky="ew", padx=3, pady=3)

    def _lk_copy(self, text, what):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.toast(f"Đã copy {what}")

    def _lk_copy_all(self):
        items = self._lk_items()
        if items:
            self._lk_copy("\n".join(x["url"] for x in items), f"{len(items)} link")

    def _lk_cut_all(self):
        items = self._lk_items()
        if items:
            self.enqueue([x["url"] for x in reversed(items)])

    def _lk_load_thumb(self, key, url):
        def work():
            try:
                import httpx
                from PIL import Image, ImageOps
                r = httpx.get(url, timeout=15, follow_redirects=True)
                im = ImageOps.fit(Image.open(io.BytesIO(r.content)).convert("RGB"), (288, 162))
                self.q.put(("lk_thumb", (key, ctk.CTkImage(im, im, size=(144, 81)))))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _lk_handle(self, kind, data):
        if kind == "lk_thumb":
            key, img = data
            self._lk_imgs[key] = img
            lbl = self._lk_labels.get(key)
            if lbl is not None and lbl.winfo_exists():
                lbl.configure(image=img)
        elif kind == "lk_refresh":
            if self._lk_current:
                self._lk_render()
            if data:
                self.notify("Kho link", f"Đã lấy thông tin {data} link")


def _open(url):
    webbrowser.open(url)
