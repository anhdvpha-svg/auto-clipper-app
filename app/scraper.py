"""Search TikTok, Instagram and X with the user's own logged-in session (the in-app login profile).

yt-dlp cannot list these any more (TikTok hashtag pages, Instagram profiles and X timelines are unsupported or
broken), so a hidden embedded browser opens the site's own search page with the saved login, listens to the JSON
the page loads (and asks the site's own API from inside the page, where the site signs the request itself), scrolls
a little for more, and reads the results. Runs in its own process, one search at a time, like a person browsing."""
import calendar, json, os, re, subprocess, sys, tempfile, threading, time
from urllib.parse import quote

from . import config

HOOK_JS = r"""(function(){ if (window.__ac) return 'ok'; window.__ac = {json: [], n: 0};
 var keep = function(u, t){ try { if (!t || t.length > 8e6 || t[0] !== '{' && t[0] !== '[') return;
   if (!/api|graphql|search|tags|profile|item_list|timeline/i.test(u || '')) return;
   window.__ac.json.push(JSON.parse(t)); window.__ac.n++; } catch (e) {} };
 var of = window.fetch;
 window.fetch = function(){ var a = arguments; return of.apply(this, a).then(function(r){
   try { var u = (a[0] && a[0].url) || String(a[0]); r.clone().text().then(function(t){ keep(u, t); }); } catch (e) {}
   return r; }); };
 var oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
 XMLHttpRequest.prototype.open = function(m, u){ this.__u = u; return oo.apply(this, arguments); };
 XMLHttpRequest.prototype.send = function(){ var x = this; x.addEventListener('load', function(){
   try { if (!x.responseType || x.responseType === 'text') keep(x.__u, x.responseText); } catch (e) {} });
   return os.apply(this, arguments); };
 return 'ok'; })()"""

# the site's own search API, called from inside its page (cookies + the site's request signing apply)
API_JS = {
    "tiktok": r"""(async function(q, n){ for (var off = 0; off < n; off += 12) { try {
        var r = await fetch('/api/search/item/full/?aid=1988&count=12&keyword=' + encodeURIComponent(q) + '&offset=' + off,
                            {credentials: 'include'});
        var t = await r.text(); if (t && t[0] === '{') window.__ac.json.push(JSON.parse(t)); } catch (e) { break; }
        await new Promise(function(res){ setTimeout(res, 900); }); } })(%s, %d); 'started'""",
    "instagram": r"""(async function(q, kind){ var h = {'x-ig-app-id': '936619743392459', 'x-requested-with': 'XMLHttpRequest'};
        var urls = kind === 'user' ? ['/api/v1/users/web_profile_info/?username=' + encodeURIComponent(q)]
                 : ['/api/v1/tags/web_info/?tag_name=' + encodeURIComponent(q.replace(/[^\w]/g, '').toLowerCase()),
                    '/api/v1/fbsearch/web/top_serp/?query=' + encodeURIComponent(q)];
        for (var i = 0; i < urls.length; i++) { try { var r = await fetch(urls[i], {headers: h, credentials: 'include'});
          var t = await r.text(); if (t && t[0] === '{') window.__ac.json.push(JSON.parse(t)); } catch (e) {} } })(%s, %s); 'started'""",
}

DOM_JS = r"""(function(re){ var R = new RegExp(re), seen = {}, out = [];
 document.querySelectorAll('a[href]').forEach(function(a){ var h = a.href.split('?')[0]; if (!R.test(h) || seen[h]) return;
   seen[h] = 1; var card = a.closest('article') || a.parentElement && a.parentElement.parentElement || a;
   var img = a.querySelector('img') || (card.querySelector && card.querySelector('img'));
   var vid = card.querySelector && card.querySelector('video,[data-testid="videoPlayer"]');
   out.push({url: h, text: ((img && img.alt) || (card.innerText || '')).slice(0, 400), thumb: img ? img.src : null,
             video: !!vid}); });
 return JSON.stringify(out.slice(0, 120)); })(%s)"""

LINK_RE = {"tiktok": r"/video/\d+$", "instagram": r"/(reel|p)/[\w-]+/?$", "x": r"/status/\d+$"}


def jobs_for(site, query, kind="search"):
    """URL to open for a keyword search / a hashtag / a profile."""
    q = query.strip().lstrip("@#")
    if site == "tiktok":
        return f"https://www.tiktok.com/@{q}" if kind == "user" else f"https://www.tiktok.com/search/video?q={quote(query)}"
    if site == "instagram":
        if kind == "user":
            return f"https://www.instagram.com/{q}/reels/"
        return f"https://www.instagram.com/explore/search/keyword/?q={quote(query)}"
    if site == "x":
        if kind == "user":
            return f"https://x.com/{q}/media"
        return f"https://x.com/search?q={quote(query + ' filter:native_video')}&src=typed_query&f=top"
    raise ValueError(site)


# ---------------- turning the site's JSON into results ----------------

def _walk(o, hit):
    stack = [o]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if hit(x):
                yield x
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)


def _int(v):
    try:
        if isinstance(v, str):
            v = v.replace(",", "").strip()
            mult = 1
            if v[-1:].upper() in "KMB" and v[:-1]:
                mult = {"K": 1e3, "M": 1e6, "B": 1e9}[v[-1].upper()]
                v = v[:-1]
            return int(float(v) * mult)
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def parse_tiktok(blobs):
    out = {}
    for b in blobs:
        for it in _walk(b, lambda d: "id" in d and "desc" in d and ("stats" in d or "statsV2" in d) and "author" in d):
            a = it.get("author") or {}
            user = a.get("uniqueId") if isinstance(a, dict) else str(a)
            if not user:
                continue
            st = it.get("statsV2") or it.get("stats") or {}
            v = it.get("video") or {}
            url = f"https://www.tiktok.com/@{user}/video/{it['id']}"
            out[url] = {"id": str(it["id"]), "url": url, "title": it.get("desc") or "(TikTok)", "channel": user,
                        "platform": "TikTok", "views": _int(st.get("playCount")), "likes": _int(st.get("diggCount")),
                        "comments": _int(st.get("commentCount")), "ts": _int(it.get("createTime")),
                        "duration": _int(v.get("duration")), "thumb": v.get("cover") or v.get("originCover"),
                        "caption": it.get("desc") or "", "live": False}
    return list(out.values())


def parse_instagram(blobs):
    out = {}
    for b in blobs:
        for m in _walk(b, lambda d: isinstance(d.get("code") or d.get("shortcode"), str)
                       and ("taken_at" in d or "taken_at_timestamp" in d)):
            video = m.get("media_type") == 2 or m.get("product_type") == "clips" or m.get("is_video") \
                or bool(m.get("video_versions"))
            if not video:
                continue
            code = m.get("code") or m.get("shortcode")
            cap = m.get("caption")
            if isinstance(cap, dict):
                cap = cap.get("text")
            if not cap:
                edges = ((m.get("edge_media_to_caption") or {}).get("edges") or [])
                cap = edges[0]["node"]["text"] if edges else ""
            user = (m.get("user") or m.get("owner") or {}).get("username", "")
            thumb = m.get("display_url") or m.get("thumbnail_src")
            cands = ((m.get("image_versions2") or {}).get("candidates") or [])
            if cands:
                thumb = cands[min(1, len(cands) - 1)].get("url")
            url = f"https://www.instagram.com/reel/{code}/"
            out[url] = {"id": code, "url": url, "title": (cap or "(Instagram Reel)").split("\n")[0][:150],
                        "caption": cap or "", "channel": user, "platform": "Instagram",
                        "views": _int(m.get("play_count") or m.get("view_count") or m.get("video_view_count")
                                      or m.get("ig_play_count")),
                        "likes": _int(m.get("like_count") or (m.get("edge_liked_by") or {}).get("count")),
                        "comments": _int(m.get("comment_count") or (m.get("edge_media_to_comment") or {}).get("count")),
                        "ts": _int(m.get("taken_at") or m.get("taken_at_timestamp")),
                        "duration": _int(m.get("video_duration")), "thumb": thumb, "live": False}
    return list(out.values())


def parse_x(blobs):
    out = {}
    for b in blobs:
        for t in _walk(b, lambda d: "rest_id" in d and isinstance(d.get("legacy"), dict)
                       and "full_text" in d["legacy"]):
            lg = t["legacy"]
            media = ((lg.get("extended_entities") or {}).get("media") or [])
            vids = [m for m in media if m.get("type") in ("video", "animated_gif")]
            if not vids:
                continue
            u = ((t.get("core") or {}).get("user_results") or {}).get("result") or {}
            screen = (u.get("legacy") or {}).get("screen_name") or (u.get("core") or {}).get("screen_name") or "i"
            ts = None
            try:
                ts = calendar.timegm(time.strptime(lg.get("created_at", ""), "%a %b %d %H:%M:%S +0000 %Y"))
            except ValueError:
                pass
            dur = ((vids[0].get("video_info") or {}).get("duration_millis") or 0) / 1000 or None
            url = f"https://x.com/{screen}/status/{t['rest_id']}"
            text = re.sub(r"\s*https://t\.co/\S+$", "", lg.get("full_text", ""))
            out[url] = {"id": t["rest_id"], "url": url, "title": text[:150] or "(X video)", "caption": text,
                        "channel": screen, "platform": "X", "views": _int((t.get("views") or {}).get("count")),
                        "likes": _int(lg.get("favorite_count")), "comments": _int(lg.get("reply_count")), "ts": ts,
                        "duration": dur, "thumb": vids[0].get("media_url_https"), "live": False}
    return list(out.values())


PARSE = {"tiktok": parse_tiktok, "instagram": parse_instagram, "x": parse_x}
PLATFORM = {"tiktok": "TikTok", "instagram": "Instagram", "x": "X"}


def from_dom(site, links):
    out = []
    for d in links:
        if site == "x" and not d.get("video"):
            continue
        txt = (d.get("text") or "").strip()
        views = None
        m = re.search(r"([\d.,]+\s*[KMB]?)\s*(?:views|lượt xem|plays)", txt, re.I) or \
            (re.match(r"^\s*([\d.,]+[KMB]?)\s*$", txt.split("\n")[0]) if txt else None)
        if m:
            views = _int(m.group(1).replace(" ", ""))
        url = d["url"].rstrip("/") + ("/" if site == "instagram" else "")
        words = [l for l in txt.split("\n") if l.strip() and not re.match(r"^[\d.,]+\s*[KMB]?$", l.strip())]
        out.append({"id": url.rstrip("/").rsplit("/", 1)[-1], "url": url,
                    "title": (words[0][:150] if words else "") or f"({PLATFORM[site]})", "caption": txt,
                    "channel": "", "platform": PLATFORM[site], "views": views, "likes": None, "comments": None,
                    "ts": None, "duration": None, "thumb": d.get("thumb"), "live": False})
    return out


# ---------------- the browsing process ----------------

def run_scrape_process(job_path, out_path):
    """Entry point of the child process (run_app.py --scrape JOB OUT). Writes {"items": [...], "error": ...}."""
    import webview
    job = json.load(open(job_path, encoding="utf-8"))
    storage = os.path.join(config.data_dir(), "webview")
    os.makedirs(storage, exist_ok=True)
    result = {"items": [], "error": None, "blobs": 0, "dom": 0}
    if job.get("html"):
        win = webview.create_window("Auto Clipper", html=job["html"], hidden=True)
    else:
        win = webview.create_window("Auto Clipper · đang tìm", job["url"], hidden=not job.get("show"),
                                    width=1100, height=850)

    def work():
        site, n = job["site"], int(job.get("n", 20))
        try:
            t0 = time.time()
            while time.time() - t0 < 30:  # page ready?
                try:
                    if win.evaluate_js("document.readyState") == "complete":
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            time.sleep(job.get("settle", 3))
            win.evaluate_js(HOOK_JS)
            if site in API_JS and job.get("query") and not (site == "tiktok" and job.get("kind") == "user"):
                arg2 = n if site == "tiktok" else json.dumps(job.get("kind", "search"))
                win.evaluate_js(API_JS[site] % (json.dumps(job["query"]), arg2))
            for _ in range(int(job.get("scrolls", 5))):
                time.sleep(1.6)
                win.evaluate_js("window.scrollBy(0, Math.max(1200, window.innerHeight * 1.5)); 1")
            time.sleep(2.5)
            blobs = json.loads(win.evaluate_js("JSON.stringify(window.__ac ? window.__ac.json : [])") or "[]")
            links = json.loads(win.evaluate_js(DOM_JS % json.dumps(LINK_RE.get(site, "/video/"))) or "[]")
            items = PARSE[site](blobs) if site in PARSE else []
            have = {x["url"].rstrip("/") for x in items}
            items += [x for x in from_dom(site, links) if x["url"].rstrip("/") not in have]
            result.update(items=items[: max(n, 1) * 2], blobs=len(blobs), dom=len(links))
            if not items:
                url = (win.get_current_url() or "").lower()
                if "login" in url or "accounts/login" in url or "/i/flow/login" in url:
                    result["error"] = "login"
                else:
                    result["error"] = "empty"
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
        finally:
            json.dump(result, open(out_path, "w", encoding="utf-8"), ensure_ascii=False)
            try:
                win.destroy()
            except Exception:
                pass

    threading.Thread(target=work, daemon=True).start()
    webview.start(private_mode=False, storage_path=storage)
    return 0


_LOCK = threading.Lock()


def scrape(site, query, n=20, kind="search", timeout=120, html=None):
    """Run one browsing job in a child process; returns (items, error) with error in (None, 'login', 'empty', ...)."""
    job = {"site": site, "query": query, "n": n, "kind": kind}
    if html:
        job.update(html=html, settle=0.5, scrolls=1)
    else:
        job["url"] = jobs_for(site, query, kind)
    d = tempfile.mkdtemp(prefix="ac_scrape_")
    jp, op = os.path.join(d, "job.json"), os.path.join(d, "out.json")
    json.dump(job, open(jp, "w", encoding="utf-8"), ensure_ascii=False)
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--scrape", jp, op]
    else:
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_app.py"),
               "--scrape", jp, op]
    with _LOCK:  # one browser at a time: same profile, and gentle on the site
        try:
            subprocess.run(cmd, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return [], "timeout"
        except Exception as e:
            return [], str(e)
    try:
        r = json.load(open(op, encoding="utf-8"))
    except Exception:
        return [], "no-output"
    return r.get("items") or [], r.get("error")
