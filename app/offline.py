"""Clip selection without any AI service: transcript heuristics + audio/visual intensity.

Weaker than an AI model (it can't judge whether a story lands), but free and private."""
import re, unicodedata
from collections import Counter

import numpy as np

HOOK_WORDS = [
    # Vietnamese
    "bí mật", "sai lầm", "không ai", "tại sao", "vì sao", "sự thật", "đừng", "không bao giờ", "quan trọng nhất",
    "bài học", "triệu", "tỷ", "lần đầu", "thất bại", "thành công", "cách", "mẹo", "kinh nghiệm", "nhất",
    "hãy tưởng tượng", "bạn có biết", "điều gì", "thật ra", "thực ra", "sốc",
    # English
    "secret", "mistake", "nobody", "never", "why", "truth", "the best", "the worst", "how to", "lesson",
    "million", "billion", "first time", "failed", "imagine", "did you know", "actually", "crazy", "rule",
]
FILLER_START = ("ờ", "à", "ừ", "ừm", "um", "uh", "so ", "and ", "but ", "và ", "nhưng ", "thì ", "rồi ", "ok", "okay",
                "vâng", "dạ", "yeah", "xin chào", "chào các bạn", "chào mừng", "hello", "hi ", "hey ", "welcome",
                "cảm ơn", "thank", "đăng ký", "subscribe", "like và", "hẹn gặp lại")
STOP = set("""và là của có không những được cho với các một này đó thì mà khi để người cũng như rất đã sẽ ở trong
ra vào lại nên bạn mình tôi anh em chị nó họ chúng ta thế nào gì nhiều hơn rồi vậy nhé nha ạ ờ à ừ
the a an and or but of to in on for with is are was were be been it this that you i we they he she my your
our their so just like really very what when how about from at as not do does did have has had will would can
could""".split())


def fold(s):
    s = unicodedata.normalize("NFKD", s.replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _hook_score(text):
    t = text.lower()
    sc = 0.0
    sc += 1.2 * sum(w in t for w in HOOK_WORDS)
    sc += 1.0 if "?" in text else 0
    sc += 0.8 if re.search(r"\d", text) else 0
    sc += 0.5 if "!" in text else 0
    if t.strip().startswith(FILLER_START):
        sc -= 2.0
    return sc


def _loudness_at(db, step, s, e):
    if db is None or not len(db):
        return 0.0
    seg = db[int(s / step): max(int(s / step) + 1, int(e / step))]
    return float(np.mean(seg)) if len(seg) else 0.0


def select_talk(transcript, opts, db=None, step=0.25):
    segs = transcript["segments"]
    lo, hi = opts["min_len"], opts["max_len"]
    target = (lo + hi) / 2
    topic = [w for w in fold(opts.get("topic") or "").split() if w not in STOP and len(w) > 2]
    cands = []
    for i, s0 in enumerate(segs):
        txt0 = s0["text"].strip()
        if not txt0 or txt0.lower().startswith(FILLER_START):
            continue
        j, text = i, [txt0]
        while j + 1 < len(segs) and segs[j + 1]["end"] - s0["start"] <= hi:
            j += 1
            text.append(segs[j]["text"])
            dur = segs[j]["end"] - s0["start"]
            ends_well = bool(re.search(r"[.!?…]$", segs[j]["text"].strip()))
            if dur >= lo and (ends_well or dur >= target):
                full = " ".join(text)
                words = len(full.split())
                cand = {
                    "start": s0["start"], "end": segs[j]["end"], "text": full, "first": txt0,
                    "hook": _hook_score(txt0) * 2 + _hook_score(full) * 0.3,
                    "density": words / dur,
                    "loud": _loudness_at(db, step, s0["start"], segs[j]["end"]),
                    "ends_well": ends_well,
                    "topic": sum(fold(full).count(w) for w in topic) if topic else 0,
                }
                cands.append(cand)
                if dur >= target and ends_well:
                    break
    if not cands:
        return []
    if topic:
        matched = [c for c in cands if c["topic"] > 0]
        if not matched:
            return []
        cands = matched

    def z(key):
        v = np.array([c[key] for c in cands], dtype=float)
        return (v - v.mean()) / (v.std() + 1e-6)
    score = 1.0 * z("hook") + 0.5 * z("density") + 0.4 * z("loud") + (2.0 * z("topic") if topic else 0)
    score += np.array([0.4 if c["ends_well"] else -0.4 for c in cands])
    return _pick(cands, score, opts, transcript.get("duration") or segs[-1]["end"], kind="talk")


def select_visual(highlights, shots, opts, duration):
    """highlights: list of (score, start, end); clip lengths fitted to opts and snapped to shot cuts."""
    lo, hi = opts["min_len"], min(opts["max_len"], 30)
    cuts = sorted({s["start"] for s in shots} | {s["end"] for s in shots})
    cands = []
    for sc, s, e in highlights:
        if e - s > hi:  # keep the loudest part: centre the window on the stretch
            mid = (s + e) / 2
            s, e = mid - hi / 2, mid + hi / 2
        if e - s < lo:
            e = min(duration, s + lo)
        if cuts:
            s = min(cuts, key=lambda c: abs(c - s))
            later = [c for c in cuts if c >= e - 1.5]
            e = later[0] if later else e
        if e - s >= max(5, lo * 0.6):
            cands.append({"start": s, "end": e, "text": "", "first": "", "raw": sc})
    if not cands:
        return []
    score = np.array([c["raw"] for c in cands])
    return _pick(cands, score, opts, duration, kind="visual")


def _pick(cands, score, opts, duration, kind):
    n = opts["num_clips"] or max(1, min(10, round(duration / 60 / 6)))
    order = np.argsort(-score)
    chosen = []
    for idx in order:
        c = cands[idx]
        if all(c["end"] <= d["start"] or c["start"] >= d["end"] for d in chosen):
            c["z"] = float(score[idx])
            chosen.append(c)
        if len(chosen) >= n:
            break
    zs = [c["z"] for c in chosen]
    zmax, zmin = max(zs), min(zs)
    out = []
    for c in chosen:
        rel = (c["z"] - zmin) / (zmax - zmin + 1e-6) if len(chosen) > 1 else 0.7
        total = int(round(55 + 30 * rel))
        out.append(_make_clip(c, total, kind, opts))
    return out


def _keywords(text, k=4):
    words = [w for w in re.findall(r"[\wÀ-ỹ]+", text.lower()) if len(w) > 2 and w not in STOP and not w.isdigit()]
    return [fold(w) for w, _ in Counter(words).most_common(k)]


def clean_title(t):
    """'Call of Duty MW4 - Official Warzone Trailer (4K) [HD]' -> 'Call of Duty MW4'."""
    t = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", t or "")
    t = re.split(r"\s[-|–—:]\s|\s#", t)[0]
    t = re.sub(r"\b(official|trailer|video|teaser|full|hd|4k|mv|lyrics?)\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -|,.") or (t or "").strip()


def _hook(text, n=8):
    """A hook line without a trailing '...': whole sentence if short, else cut at a natural break."""
    words = re.sub(r"\s+", " ", text).strip().split()
    if len(words) <= n:
        return " ".join(words).rstrip(",.;:")
    cut = words[:n]
    for i in range(n - 1, 2, -1):          # prefer ending after a comma / clause word
        if cut[i].endswith((",", ";", ":")):
            cut = cut[:i + 1]
            break
    return " ".join(cut).rstrip(",.;:")


def _short(text, n=8):
    words = re.sub(r"\s+", " ", text).strip().split()
    s = " ".join(words[:n])
    return s.rstrip(",.;:") + ("..." if len(words) > n else "")


def _make_clip(c, total, kind, opts):
    first = c.get("first") or ""
    if kind == "talk":
        title = _short(first, 10) or "Clip"
        hook = _hook(first, 8)
        tags = ["#" + w for w in _keywords(c["text"])] + ["#shorts"]
        reason = "Chọn tự động (offline): câu mở đầu có tín hiệu hút người xem, lời nói dày, âm lượng cao."
    else:
        title = clean_title(opts.get("video_title")) or "Highlight"
        hook = _hook(title, 8) or "Đoạn cao trào nhất"
        tags = ["#" + w for w in _keywords(opts.get("video_title") or "")] + ["#highlight", "#shorts"]
        reason = "Chọn tự động (offline): đoạn ồn và nhiều chuyển động nhất."
    caption = title
    return {
        "title": title, "start": round(c["start"], 2), "end": round(c["end"], 2),
        "hook_title": hook, "opening_line": first,
        "score": {"total": total, "hook": int(total * 0.3), "emotion": int(total * 0.25),
                  "value": int(total * 0.25), "standalone": int(total * 0.2)},
        "reason": reason,
        "caption_tiktok": caption, "caption_reels": caption, "caption_shorts": _short(title, 10),
        "hashtags": tags[:6],
    }
