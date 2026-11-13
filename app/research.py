"""Find source videos to clip: keyword search per platform and watched channels (YouTube, Twitch, Kick, TikTok,
Instagram, X, Dailymotion, Bilibili, Rumble...).

Everything goes through yt-dlp in 'flat' mode (metadata only, nothing downloaded), then the most promising
results get a full metadata pass for their upload time and engagement, and every result gets a Hot score."""
import base64, json, math, os, re, subprocess, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor

PERIODS = {"hour": 1, "today": 2, "week": 3, "month": 4, "year": 5, "any": 0}
LENGTHS = {"any": 0, "short": 1, "long": 2, "medium": 3}          # <4 min, >20 min, 4-20 min
SORTS = {"relevance": 0, "date": 2, "views": 3}


class ResearchError(Exception):
    pass


# platform key -> label, how a keyword is used ("search", "tag" = hashtag page, "channel" = the words are channel
# names, None = channels only), and a hint shown under the chips
# which in-app login (platform_login.SITES) each platform uses, and whether it is required to scan it
LOGIN = {"tiktok": ("tiktok", True), "instagram": ("instagram", True), "x": ("x", True),
         "twitch": ("twitch", False), "yt_long": ("youtube", False), "yt_shorts": ("youtube", False)}

PLATFORMS = {
    "yt_long":   {"label": "YouTube video dài", "kw": "search", "name": "YouTube"},
    "yt_shorts": {"label": "YouTube Shorts", "kw": "search", "name": "YouTube Shorts"},
    "tiktok":    {"label": "TikTok", "kw": "tag", "name": "TikTok",
                  "hint": "TikTok: tìm theo hashtag, cần chọn trình duyệt đã đăng nhập TikTok trong Cài đặt."},
    "instagram": {"label": "Instagram Reels", "kw": "tag", "name": "Instagram",
                  "hint": "Instagram: tìm theo hashtag, cần đăng nhập (trình duyệt hoặc cookies.txt trong Cài đặt)."},
    "twitch":    {"label": "Twitch", "kw": "channel", "name": "Twitch",
                  "hint": "Twitch/Kick không có tìm kiếm công khai: gõ tên kênh (vd: xqc, kaicenat), app lấy clip "
                          "hot 7 ngày + VOD mới."},
    "kick":      {"label": "Kick", "kw": "channel", "name": "Kick"},
    "dailymotion": {"label": "Dailymotion", "kw": "search", "name": "Dailymotion"},
    "bilibili":  {"label": "Bilibili", "kw": "search", "name": "Bilibili"},
    "x":         {"label": "X (Twitter)", "kw": "channel", "name": "X",
                  "hint": "X: gõ tên tài khoản (vd: elonmusk), cần đăng nhập X trong trình duyệt."},
    "rumble":    {"label": "Rumble", "kw": "channel", "name": "Rumble"},
}


def youtube_search_url(query, period="week", length="any", sort="views"):
    """YouTube results URL with filters (the 'sp' parameter is a small protobuf)."""
    inner = b""
    if PERIODS.get(period):
        inner += bytes([0x08, PERIODS[period]])
    inner += bytes([0x10, 1])                                        # type: video
    if LENGTHS.get(length):
        inner += bytes([0x18, LENGTHS[length]])
    raw = (bytes([0x08, SORTS[sort]]) if SORTS.get(sort) else b"") + bytes([0x12, len(inner)]) + inner
    sp = base64.urlsafe_b64encode(raw).decode()
    return f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}&sp={urllib.parse.quote(sp)}"


def platform_of(url):
    u = url.lower()
    for key, name in (("youtube.com/shorts", "YouTube Shorts"), ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
                      ("twitch.tv", "Twitch"), ("kick.com", "Kick"), ("tiktok.com", "TikTok"),
                      ("instagram.com", "Instagram"), ("x.com", "X"), ("twitter.com", "X"),
                      ("dailymotion.com", "Dailymotion"), ("bilibili.com", "Bilibili"), ("rumble.com", "Rumble"),
                      ("facebook.com", "Facebook"), ("vimeo.com", "Vimeo")):
        if key in u:
            return name
    return "Web"


def _slug_tag(q):
    return re.sub(r"[^\w]", "", q.lower().replace(" ", ""))


def _handles(q):
    """'xqc, kai cenat' -> ['xqc', 'kaicenat'] (channel names typed in the keyword box)."""
    return [re.sub(r"[^\w.-]", "", h.strip().lstrip("@").replace(" ", "")) for h in re.split(r"[,;\n]", q) if h.strip()]


def source_listing_url(url):
    """Turn a channel/profile link into the page that lists its videos, newest first."""
    url = url.strip()
    p = platform_of(url)
    if p == "YouTube" and re.search(r"youtube\.com/(@|channel/|c/|user/)[^/?#]+/?$", url):
        return url.rstrip("/") + "/videos"
    if p == "Twitch" and re.search(r"twitch\.tv/[^/?#]+/?$", url):
        return url.rstrip("/") + "/videos?filter=archives&sort=time"
    if p == "Kick" and re.search(r"kick\.com/[^/?#]+/?$", url):
        return url.rstrip("/") + "/videos"
    if p == "Bilibili" and re.search(r"space\.bilibili\.com/\d+/?$", url):
        return url.rstrip("/") + "/video"
    if p == "X" and re.search(r"(x|twitter)\.com/[^/?#]+/?$", url):
        return url.rstrip("/") + "/media"
    return url


class Researcher:
    def __init__(self, ytdlp, deno=None, browser=None, log=print, cookies=None):
        self.ytdlp, self.deno, self.browser, self.log = ytdlp, deno, browser, log
        self.cookies = cookies if cookies and os.path.isfile(cookies) else None

    def _base(self, url=""):
        from . import platform_login
        a = [self.ytdlp, "--no-warnings", "--ignore-errors", "--ignore-config"]
        if self.deno:
            a += ["--js-runtimes", f"deno:{self.deno}"]
        site_ck = platform_login.cookies_for_url(url)
        if self.cookies:
            a += ["--cookies", self.cookies]
        elif site_ck:
            a += ["--cookies", site_ck]
        elif self.browser:
            a += ["--cookies-from-browser", self.browser]
        return a

    def _json(self, args, timeout=150):
        url = next((x for x in reversed(args) if str(x).startswith("http")), "")
        try:
            r = subprocess.run(self._base(url) + args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ResearchError("Hết thời gian chờ phản hồi từ nền tảng. Thử lại sau.")
        out = r.stdout.strip()
        if not out:
            err = (r.stderr.strip().splitlines() or ["không rõ lỗi"])[-1]
            if "sign in" in err.lower() or "not a bot" in err.lower() or "login" in err.lower() \
                    or "log in" in err.lower() or "cookies" in err.lower():
                raise ResearchError(f"{platform_of(url)} yêu cầu đăng nhập: bấm nút 🔒 {platform_of(url)} ở trên để "
                                    "đăng nhập một lần trong app.")
            raise ResearchError(f"Không lấy được danh sách: {err[:200]}")
        try:
            d = json.loads(out[out.index("{"):])
        except ValueError:
            d = None
        if not d:
            if any("tiktok.com" in str(a) for a in args):
                raise ResearchError("TikTok chặn xem danh sách khi chưa đăng nhập: bấm nút 🔒 TikTok ở trên để đăng "
                                    "nhập một lần trong app rồi quét lại.")
            raise ResearchError("Nền tảng không trả về dữ liệu (bị chặn hoặc link sai).")
        return d

    # ---------- listing ----------
    def search(self, query, period="week", length="any", sort="views", n=30):
        """YouTube keyword search (long videos)."""
        if not query.strip():
            raise ResearchError("Nhập từ khoá cần tìm.")
        d = self._json(["--flat-playlist", "-J", "--playlist-end", str(n),
                        youtube_search_url(query, period, length, sort)])
        return [self._norm(e, "YouTube") for e in (d.get("entries") or []) if e]

    def search_platform(self, key, query, period="week", length="any", sort="views", n=30):
        """Keyword search on one platform. Raises ResearchError with a user-facing hint when it can't."""
        q = query.strip()
        if not q:
            raise ResearchError("Nhập từ khoá cần tìm.")
        P = PLATFORMS[key]
        if key == "yt_long":
            return self.search(q, period, length if length != "short" else "any", sort, n)
        if key == "yt_shorts":
            d = self._json(["--flat-playlist", "-J", "--playlist-end", str(n * 2),
                            youtube_search_url(q + " #shorts", period, "short", sort)])
            out = []
            for e in d.get("entries") or []:
                if e and (e.get("duration") or 0) <= 180:
                    x = self._norm(e, "YouTube Shorts")
                    x["url"] = f"https://www.youtube.com/shorts/{e['id']}"
                    out.append(x)
            return out[:n]
        if key == "dailymotion":
            return self._dailymotion(q, period, sort, n)
        if key == "bilibili":
            d = self._json(["--flat-playlist", "-J", f"bilisearch{n}:{q}"])
            return [self._norm(e, "Bilibili") for e in d.get("entries") or [] if e]
        if key == "tiktok":
            return self.channel(f"https://www.tiktok.com/tag/{_slug_tag(q)}", n, platform="TikTok")
        if key == "instagram":
            return self.channel(f"https://www.instagram.com/explore/tags/{_slug_tag(q)}/", n, platform="Instagram")
        if P["kw"] == "channel":
            urls = []
            for h in _handles(q)[:8]:
                if key == "twitch":
                    urls += [f"https://www.twitch.tv/{h}/clips?filter=clips&range=7d", f"https://www.twitch.tv/{h}"]
                elif key == "kick":
                    urls.append(f"https://kick.com/{h}")
                elif key == "x":
                    urls.append(f"https://x.com/{h}")
                elif key == "rumble":
                    urls.append(f"https://rumble.com/c/{h}")
            items, errors = self.channels(urls, max(5, n // max(1, len(urls))))
            if not items and errors:
                raise ResearchError(errors[0])
            return items
        raise ResearchError(f"{P['label']}: chưa hỗ trợ tìm theo từ khoá.")

    def _dailymotion(self, q, period, sort, n):
        import httpx
        params = {"search": q, "limit": min(100, n), "sort": "recent" if sort == "date" else "relevance",
                  "fields": "id,title,views_total,duration,created_time,owner.username,thumbnail_360_url,url,likes_total"}
        days = {"hour": 1 / 24, "today": 1, "week": 7, "month": 30, "year": 365}.get(period)
        if days:
            params["created_after"] = int(time.time() - days * 86400)
        try:
            r = httpx.get("https://api.dailymotion.com/videos", params=params, timeout=20)
            r.raise_for_status()
        except Exception as e:
            raise ResearchError(f"Dailymotion không phản hồi ({e}).")
        out = []
        for e in r.json().get("list", []):
            out.append({"id": e["id"], "url": e.get("url") or f"https://www.dailymotion.com/video/{e['id']}",
                        "title": e.get("title") or "", "channel": e.get("owner.username") or "",
                        "platform": "Dailymotion", "channel_id": e.get("owner.username"),
                        "views": e.get("views_total"), "duration": e.get("duration"), "ts": e.get("created_time"),
                        "likes": e.get("likes_total"), "comments": None, "thumb": e.get("thumbnail_360_url"),
                        "live": False})
        return out

    def search_many(self, keys, queries, period, length, sort, n, progress=None):
        """Every keyword on every chosen platform, in parallel. Returns (items, errors per platform)."""
        jobs = [(k, q) for k in keys for q in queries]
        items, errors, seen = [], [], set()
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(self.search_platform, k, q, period, length, sort, n): (k, q) for k, q in jobs}
            for f, (k, q) in futs.items():
                try:
                    got = f.result()
                except ResearchError as e:
                    errors.append(f"{PLATFORMS[k]['label']}: {e}")
                    continue
                except Exception as e:
                    errors.append(f"{PLATFORMS[k]['label']}: {e}")
                    continue
                for x in got:
                    if x["url"] not in seen:
                        seen.add(x["url"])
                        x["query"] = q
                        items.append(x)
                if progress:
                    progress(f"{PLATFORMS[k]['label']}: {len(got)} video cho “{q}”")
        return items, errors

    def channel(self, url, n=15, platform=None):
        lst = source_listing_url(url)
        plat = platform or platform_of(url)
        d = self._json(["--flat-playlist", "-J", "--playlist-end", str(n), lst])
        if d.get("_type") != "playlist":   # a single video link
            return [self._norm(d, plat)]
        entries = [e for e in (d.get("entries") or []) if e]
        if not entries:
            if plat in ("Instagram", "TikTok", "X"):
                raise ResearchError(f"{plat} không trả về video vì chưa đăng nhập: bấm nút 🔒 {plat} ở trên để đăng "
                                    f"nhập một lần trong app (app nhớ phiên cho lần sau).")
            raise ResearchError(f"Không lấy được video từ {url} (nền tảng chặn hoặc kênh trống).")
        out = [self._norm(e, plat, d.get("uploader") or d.get("channel") or d.get("title")) for e in entries]
        # channel median: what this channel normally gets, for the "vượt trội" (outlier) score
        views = sorted(x["views"] for x in out if x.get("views"))
        if len(views) >= 4:
            med = views[len(views) // 2]
            for x in out:
                x["ch_median"] = med
        return out

    def channels(self, urls, n=10):
        """Scan several channels in parallel; errors are reported per channel, results merged."""
        results, errors = [], []
        expanded = []
        for u in (u.strip() for u in urls if u.strip()):
            m = re.match(r"(https?://(?:www\.)?twitch\.tv/[^/?#]+)/?$", u)
            if m and f"{m.group(1)}/clips?filter=clips&range=7d" not in expanded:
                expanded.append(f"{m.group(1)}/clips?filter=clips&range=7d")  # the week's top clips too
            expanded.append(u)
        urls = list(dict.fromkeys(expanded))
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(self.channel, u, n): u for u in urls}
            for f, u in futs.items():
                try:
                    results += f.result()
                except ResearchError as e:
                    errors.append(f"{u}: {e}")
        return results, errors

    @staticmethod
    def _norm(e, platform, channel=None):
        url = e.get("webpage_url") or e.get("url") or ""
        if platform == "YouTube" and e.get("id") and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={e['id']}"
        thumbs = e.get("thumbnails") or []
        thumb = e.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None)
        if platform.startswith("YouTube") and e.get("id"):
            thumb = f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"
        ts = e.get("timestamp") or e.get("release_timestamp")
        if not ts and e.get("upload_date"):
            try:
                ts = time.mktime(time.strptime(e["upload_date"], "%Y%m%d"))
            except ValueError:
                ts = None
        return {"id": e.get("id"), "url": url, "title": e.get("title") or "(không tiêu đề)",
                "channel": e.get("channel") or e.get("uploader") or channel or "", "platform": platform,
                "channel_id": e.get("channel_id"),
                "views": e.get("view_count"), "duration": e.get("duration"), "ts": ts,
                "likes": e.get("like_count"), "comments": e.get("comment_count"), "thumb": thumb,
                "live": e.get("live_status") in ("is_live", "is_upcoming")}

    # ---------- enrichment ----------
    def enrich(self, items, top=12):
        """Upload times, channel medians (outliers) and engagement where cheap. YouTube: the channel RSS feed first
        (one light request per channel: publish time and views of its latest 15 videos), then full metadata for
        what's still missing."""
        yt = [x for x in items if x["platform"].startswith("YouTube") and x.get("channel_id")]
        self._rss(yt)
        need = [x for x in sorted(items, key=lambda x: -(x["views"] or 0)) if not x["ts"] and not x["live"]][:top]

        def one(x):
            try:
                d = self._json(["-J", "--skip-download", "--no-playlist", x["url"]], timeout=90)
            except ResearchError:
                return
            x["ts"] = d.get("timestamp") or d.get("release_timestamp") or x["ts"]
            x["likes"] = d.get("like_count") or x["likes"]
            x["comments"] = d.get("comment_count") or x["comments"]
            x["views"] = d.get("view_count") or x["views"]
            x["duration"] = d.get("duration") or x["duration"]
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(one, need))
        # channels seen several times in the results: their median is a fallback baseline
        by = {}
        for x in items:
            if x.get("views") and x.get("channel"):
                by.setdefault((x["platform"], x["channel"]), []).append(x["views"])
        for x in items:
            v = sorted(by.get((x["platform"], x.get("channel")), []))
            if not x.get("ch_median") and len(v) >= 3:
                x["ch_median"] = v[len(v) // 2]
        return items

    @staticmethod
    def _rss(items, max_channels=30):
        import calendar
        import httpx
        by_ch = {}
        for x in items:
            by_ch.setdefault(x["channel_id"], []).append(x)

        def feed(cid):
            try:
                r = httpx.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", timeout=15)
                if r.status_code != 200:
                    return {}, None
            except httpx.HTTPError:
                return {}, None
            out, views = {}, []
            for entry in re.findall(r"<entry>(.*?)</entry>", r.text, flags=re.S):
                vid = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", entry)
                pub = re.search(r"<published>([^<]+)</published>", entry)
                vw = re.search(r'<media:statistics views="(\d+)"', entry)
                if vw:
                    views.append(int(vw.group(1)))
                if vid and pub:
                    try:
                        out[vid.group(1)] = calendar.timegm(time.strptime(pub.group(1)[:19], "%Y-%m-%dT%H:%M:%S"))
                    except ValueError:
                        pass
            views.sort()
            return out, (views[len(views) // 2] if len(views) >= 4 else None)
        chans = list(by_ch)[:max_channels]
        with ThreadPoolExecutor(max_workers=6) as ex:
            for cid, (times, med) in zip(chans, ex.map(feed, chans)):
                for x in by_ch[cid]:
                    x["ts"] = x["ts"] or times.get(x["id"])
                    if med:
                        x["ch_median"] = med


# ---------- scoring ----------

def _rank(vals):
    """Percentile rank 0..1 (None -> 0.3, a neutral-low value)."""
    known = sorted(v for v in vals if v is not None)
    if not known:
        return [0.3] * len(vals)
    out = []
    for v in vals:
        if v is None:
            out.append(0.3)
        else:
            lo = sum(1 for k in known if k < v)
            out.append(lo / max(1, len(known) - 1) if len(known) > 1 else 0.7)
    return out


def score(items, done_ids=()):
    """Hot score 0-100: view velocity (views/hour) 50%, recency 20%, clip-friendly length 15%, engagement 15%."""
    now = time.time()
    for x in items:
        age_h = (now - x["ts"]) / 3600 if x.get("ts") else None
        x["age_h"] = age_h
        x["velocity"] = (x["views"] / max(age_h, 2)) if (x.get("views") and age_h) else None
        x["engagement"] = ((x.get("likes") or 0) + 2 * (x.get("comments") or 0)) / x["views"] \
            if x.get("views") and (x.get("likes") or x.get("comments")) else None
        d = x.get("duration") or 0
        # long talks/streams hold many clippable moments; under 2 min there is little to cut
        x["fit"] = 0.1 if d < 120 else 0.5 if d < 480 else 1.0 if d < 4 * 3600 else 0.8
    vel = _rank([math.log10(x["velocity"] + 1) if x["velocity"] else None for x in items])
    rec = _rank([-x["age_h"] if x["age_h"] is not None else None for x in items])
    eng = _rank([x["engagement"] for x in items])
    for x, v, r, g in zip(items, vel, rec, eng):
        # with no upload time, fall back to raw views for the velocity slot
        x["hot"] = int(round(100 * (0.5 * v + 0.2 * r + 0.15 * x["fit"] + 0.15 * g)))
        tags = []
        if x["age_h"] is not None and x["age_h"] < 72 and v >= 0.75:
            tags.append("Đang lên")
        if x["age_h"] is not None and x["age_h"] < 24:
            tags.append("Mới")
        if (x.get("duration") or 0) >= 1200:
            tags.append("Dài")
        if x["live"]:
            tags.append("Đang live")
        if x.get("id") in done_ids or x.get("url") in done_ids:
            tags.append("Đã cắt")
        x["tags"] = tags
    if not any(x["velocity"] for x in items):  # no times at all: rank by views
        vr = _rank([x.get("views") for x in items])
        for x, v in zip(items, vr):
            x["hot"] = int(round(100 * (0.65 * v + 0.2 * x["fit"] + 0.15 * 0.3)))
    for x in items:
        # outlier: this video vs what its channel normally gets - the strongest "it's going viral" signal
        med = x.get("ch_median")
        x["outlier"] = round(x["views"] / med, 1) if med and x.get("views") else None
        if x["outlier"] and x["outlier"] >= 3:
            x["tags"].insert(0, f"Vượt trội x{x['outlier']:g}")
            x["hot"] = min(100, x["hot"] + min(15, int(3 * math.log2(x["outlier"]))))
    return sorted(items, key=lambda x: -x["hot"])


MAX_AGE_H = {"hour": 1, "today": 24, "week": 24 * 7, "month": 24 * 31, "year": 24 * 366}
LENGTH_RANGE = {"short": (0, 240), "medium": (240, 1200), "long": (1200, 10 ** 7)}
SORT_KEYS = {"hot": lambda x: -x["hot"], "views": lambda x: -(x.get("views") or 0),
             "velocity": lambda x: -(x.get("velocity") or 0), "date": lambda x: -(x.get("ts") or 0),
             "outlier": lambda x: -(x.get("outlier") or 0)}


def apply_filters(items, f):
    """Viral filters on the scored list: min views, min views/hour, max age, length, exclude words, outliers only."""
    out = []
    excl = [w.strip().lower() for w in (f.get("exclude") or "").split(",") if w.strip()]
    for x in items:
        if f.get("min_views") and (x.get("views") or 0) < f["min_views"]:
            continue
        if f.get("min_vph") and (x.get("velocity") or 0) < f["min_vph"]:
            continue
        lim = MAX_AGE_H.get(f.get("period"))
        if lim and x.get("age_h") is not None and x["age_h"] > lim * 1.05:
            continue
        rng = LENGTH_RANGE.get(f.get("length"))
        if rng and x.get("duration") and not rng[0] <= x["duration"] < rng[1] and not x["platform"].endswith("Shorts"):
            continue
        if excl and any(w in (x.get("title") or "").lower() for w in excl):
            continue
        if f.get("outliers") and not (x.get("outlier") or 0) >= 2:
            continue
        if f.get("min_hot") and x["hot"] < f["min_hot"]:
            continue
        out.append(x)
    return sorted(out, key=SORT_KEYS.get(f.get("order", "hot"), SORT_KEYS["hot"]))


def done_sources(output_root):
    """IDs/URLs already turned into projects, so results can be marked 'Đã cắt'."""
    seen = set()
    for pj in __import__("glob").glob(os.path.join(output_root, "*", "*", "project.json")):
        try:
            src = json.load(open(pj, encoding="utf-8")).get("source") or ""
        except Exception:
            continue
        if src:
            seen.add(src)
            m = re.search(r"(?:v=|youtu\.be/|/videos?/|/clip/)([\w-]{6,})", src)
            if m:
                seen.add(m.group(1))
    return seen


def human_views(n):
    if not n:
        return "–"
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}{unit}".replace(".0", "")
    return str(int(n))


def human_age(h):
    if h is None:
        return ""
    if h < 1:
        return "vừa đăng"
    if h < 24:
        return f"{int(h)} giờ trước"
    if h < 24 * 30:
        return f"{int(h / 24)} ngày trước"
    return f"{int(h / 24 / 30)} tháng trước"


def human_dur(s):
    if not s:
        return ""
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
