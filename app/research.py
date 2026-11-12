"""Find source videos to clip: keyword search (YouTube) and watched channels (YouTube, Twitch, Kick, TikTok).

Everything goes through yt-dlp in 'flat' mode (metadata only, nothing downloaded), then the most promising
results get a full metadata pass for their upload time and engagement, and every result gets a Hot score."""
import base64, json, math, os, re, subprocess, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor

PERIODS = {"hour": 1, "today": 2, "week": 3, "month": 4, "year": 5, "any": 0}
LENGTHS = {"any": 0, "short": 1, "long": 2, "medium": 3}          # <4 min, >20 min, 4-20 min
SORTS = {"relevance": 0, "date": 2, "views": 3}


class ResearchError(Exception):
    pass


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
    for key, name in (("youtube.com", "YouTube"), ("youtu.be", "YouTube"), ("twitch.tv", "Twitch"),
                      ("kick.com", "Kick"), ("tiktok.com", "TikTok"), ("instagram.com", "Instagram"),
                      ("x.com", "X"), ("twitter.com", "X")):
        if key in u:
            return name
    return "Web"


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
    return url


class Researcher:
    def __init__(self, ytdlp, deno=None, browser=None, log=print):
        self.ytdlp, self.deno, self.browser, self.log = ytdlp, deno, browser, log

    def _base(self):
        a = [self.ytdlp, "--no-warnings", "--ignore-errors", "--ignore-config"]
        if self.deno:
            a += ["--js-runtimes", f"deno:{self.deno}"]
        if self.browser:
            a += ["--cookies-from-browser", self.browser]
        return a

    def _json(self, args, timeout=150):
        try:
            r = subprocess.run(self._base() + args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ResearchError("Hết thời gian chờ phản hồi từ nền tảng. Thử lại sau.")
        out = r.stdout.strip()
        if not out:
            err = (r.stderr.strip().splitlines() or ["không rõ lỗi"])[-1]
            if "sign in" in err.lower() or "not a bot" in err.lower() or "login" in err.lower():
                raise ResearchError("Nền tảng yêu cầu đăng nhập. Vào Cài đặt → Tải video, chọn trình duyệt bạn đang "
                                    "đăng nhập rồi thử lại.")
            raise ResearchError(f"Không lấy được danh sách: {err[:200]}")
        try:
            d = json.loads(out[out.index("{"):])
        except ValueError:
            d = None
        if not d:
            if any("tiktok.com" in str(a) for a in args):
                raise ResearchError("TikTok chặn xem danh sách khi chưa đăng nhập. Vào Cài đặt → Tải video, chọn "
                                    "trình duyệt bạn đang đăng nhập TikTok rồi quét lại.")
            raise ResearchError("Nền tảng không trả về dữ liệu (bị chặn hoặc link sai).")
        return d

    # ---------- listing ----------
    def search(self, query, period="week", length="any", sort="views", n=30):
        if not query.strip():
            raise ResearchError("Nhập từ khoá cần tìm.")
        d = self._json(["--flat-playlist", "-J", "--playlist-end", str(n),
                        youtube_search_url(query, period, length, sort)])
        return [self._norm(e, "YouTube") for e in (d.get("entries") or []) if e]

    def channel(self, url, n=15):
        lst = source_listing_url(url)
        d = self._json(["--flat-playlist", "-J", "--playlist-end", str(n), lst])
        if d.get("_type") != "playlist":   # a single video link
            return [self._norm(d, platform_of(url))]
        entries = [e for e in (d.get("entries") or []) if e]
        if not entries:
            raise ResearchError(f"Không lấy được video từ {url} (nền tảng chặn hoặc kênh trống).")
        return [self._norm(e, platform_of(url), d.get("uploader") or d.get("channel") or d.get("title"))
                for e in entries]

    def channels(self, urls, n=10):
        """Scan several channels in parallel; errors are reported per channel, results merged."""
        results, errors = [], []
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(self.channel, u, n): u for u in urls if u.strip()}
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
        if platform == "YouTube" and e.get("id"):
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
        """Upload times (and engagement where cheap). YouTube: the channel RSS feed first (one light request per
        channel, gives the publish time of its latest 15 videos), then full metadata for what's still missing."""
        self._rss_times([x for x in items if x["platform"] == "YouTube" and not x["ts"] and x.get("channel_id")])
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
        return items


    @staticmethod
    def _rss_times(items):
        import calendar
        import httpx
        by_ch = {}
        for x in items:
            by_ch.setdefault(x["channel_id"], []).append(x)

        def feed(cid):
            try:
                r = httpx.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", timeout=15)
                if r.status_code != 200:
                    return {}
            except httpx.HTTPError:
                return {}
            out = {}
            for vid, pub in re.findall(r"<yt:videoId>([^<]+)</yt:videoId>.*?<published>([^<]+)</published>",
                                       r.text, flags=re.S):
                try:
                    out[vid] = calendar.timegm(time.strptime(pub[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    pass
            return out
        with ThreadPoolExecutor(max_workers=6) as ex:
            for cid, times in zip(by_ch, ex.map(feed, list(by_ch))):
                for x in by_ch[cid]:
                    x["ts"] = times.get(x["id"]) or x["ts"]


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
    return sorted(items, key=lambda x: -x["hot"])


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
