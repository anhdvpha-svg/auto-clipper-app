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


def _z(vals):
    v = np.array(vals, dtype=float)
    return (v - v.mean()) / (v.std() + 1e-6) if len(v) else v


def select_talk(transcript, opts, db=None, step=0.25, exclude=()):
    """Dense candidate windows (every sentence start x every sentence end that fits the length range)."""
    segs = transcript["segments"]
    lo, hi = opts["min_len"], opts["max_len"]
    duration = transcript.get("duration") or (segs[-1]["end"] if segs else 0)
    topic = [w for w in fold(opts.get("topic") or "").split() if w not in STOP and len(w) > 2]
    cands = []
    for i, s0 in enumerate(segs):
        txt0 = s0["text"].strip()
        if not txt0:
            continue
        filler = txt0.lower().startswith(FILLER_START)
        text = [txt0]
        for j in range(i + 1, len(segs)):
            dur = segs[j]["end"] - s0["start"]
            if dur > hi:
                break
            text.append(segs[j]["text"])
            if dur < lo:
                continue
            full = " ".join(text)
            ends_well = bool(re.search(r"[.!?…]$", segs[j]["text"].strip()))
            cands.append({
                "start": s0["start"], "end": segs[j]["end"], "text": full, "first": txt0,
                "hook": _hook_score(txt0) * 2 + _hook_score(full) * 0.3 - (3 if filler else 0),
                "density": len(full.split()) / dur,
                "loud": _loudness_at(db, step, s0["start"], segs[j]["end"]),
                "ends_well": ends_well,
                "topic": sum(fold(full).count(w) for w in topic) if topic else 0,
            })
    if topic:
        cands = [c for c in cands if c["topic"] > 0]
        if not cands:
            return []
    if not cands:  # no sentence span fits the length: fall back to even slices of the video
        return _name_from_speech(_pick([], np.array([]), opts, duration, kind="talk",
                                       bounds=sorted({x["start"] for x in segs})), segs, opts)
    score = 1.0 * _z([c["hook"] for c in cands]) + 0.5 * _z([c["density"] for c in cands]) \
        + 0.4 * _z([c["loud"] for c in cands]) + (2.0 * _z([c["topic"] for c in cands]) if topic else 0)
    score = score + np.array([0.4 if c["ends_well"] else -0.4 for c in cands])
    return _name_from_speech(_pick(cands, score, opts, duration, kind="talk", exclude=exclude,
                                   bounds=sorted({x["start"] for x in segs} | {x["end"] for x in segs})), segs, opts)


def _name_from_speech(clips, segs, opts):
    """Clips added by even slicing have no opening line yet: use the first sentence spoken inside them."""
    out = []
    for c in clips:
        if not c.get("opening_line"):
            inside = [x for x in segs if x["start"] >= c["start"] - 0.2 and x["end"] <= c["end"] + 0.2 and
                      x["text"].strip()]
            if inside:
                fresh = _make_clip({"start": c["start"], "end": c["end"], "first": inside[0]["text"].strip(),
                                    "text": " ".join(x["text"] for x in inside)}, c["score"]["total"], "talk", opts)
                c = {**c, **{k: fresh[k] for k in ("title", "hook_title", "opening_line", "caption_tiktok",
                                                   "caption_reels", "caption_shorts", "hashtags")}}
        out.append(c)
    return out


def select_visual(highlights, shots, opts, duration, exclude=()):
    """Dense windows over the whole video, cut on shot boundaries, scored by loudness/motion/cut rate.
    `highlights` (the most intense stretches) add a bonus."""
    lo, hi = opts["min_len"], opts["max_len"]
    if not shots:
        shots = [{"start": t, "end": min(duration, t + 2.0), "loud_db": -30.0, "motion": 0.0}
                 for t in np.arange(0, duration, 2.0)]
    starts = [s["start"] for s in shots]
    loud = _z([s.get("loud_db", -30) for s in shots])
    motion = _z([s.get("motion", 0) for s in shots]) if np.std([s.get("motion", 0) for s in shots]) > 0 \
        else np.zeros(len(shots))
    inten = loud + motion
    cands = []
    for i in range(len(shots)):
        for j in range(i, len(shots)):
            dur = shots[j]["end"] - shots[i]["start"]
            if dur > hi:
                break
            if dur < lo:
                continue
            w = np.array([shots[k]["end"] - shots[k]["start"] for k in range(i, j + 1)])
            mean_int = float((inten[i:j + 1] * w).sum() / w.sum())
            cuts = (j - i + 1) / dur * 10
            bonus = sum(sc for sc, hs, he in highlights if hs < shots[j]["end"] and he > shots[i]["start"]) * 0.3
            cands.append({"start": shots[i]["start"], "end": shots[j]["end"], "text": "", "first": "",
                          "raw": mean_int + 0.15 * cuts + bonus})
    if not cands:  # video shorter than the minimum length: one clip of everything
        cands = [{"start": 0.0, "end": duration, "text": "", "first": "", "raw": 0.0}]
    return _pick(cands, np.array([c["raw"] for c in cands]), opts, duration, kind="visual", exclude=exclude,
                 bounds=sorted(set(starts) | {s["end"] for s in shots}))


def _overlaps(c, others):
    return any(not (c["end"] <= d["start"] or c["start"] >= d["end"]) for d in others)


def _pick(cands, score, opts, duration, kind, exclude=(), bounds=()):
    """Best-scoring non-overlapping windows; if that leaves fewer than asked, tile the free time evenly."""
    n = opts["num_clips"] or max(1, min(10, round(duration / 60 / 6)))
    taken = [{"start": a, "end": b} for a, b in exclude]
    order = np.argsort(-score)
    chosen = []
    for idx in order:
        c = cands[idx]
        if not _overlaps(c, chosen + taken):
            c["z"] = float(score[idx])
            chosen.append(c)
        if len(chosen) >= n:
            break
    if len(chosen) < n and opts.get("num_clips"):
        chosen = _fill_by_tiling(cands, score, chosen, taken, n, opts, duration, bounds)
    if not chosen:
        return []
    zs = [c["z"] for c in chosen]
    zmax, zmin = max(zs), min(zs)
    out = []
    for c in chosen:
        rel = (c["z"] - zmin) / (zmax - zmin + 1e-6) if len(chosen) > 1 else 0.7
        out.append(_make_clip(c, int(round(55 + 30 * rel)), kind, opts))
    return out


def _fill_by_tiling(cands, score, chosen, taken, n, opts, duration, bounds):
    """Greedy picking fragments the timeline. When the user asked for N clips, cut the free time into
    equal slices (on sentence/shot boundaries) and keep the best-scoring windows inside each slice."""
    lo = opts["min_len"]
    free, cur = [], 0.0
    for b in sorted(taken, key=lambda x: x["start"]):
        if b["start"] - cur >= lo:
            free.append((cur, b["start"]))
        cur = max(cur, b["end"])
    if duration - cur >= lo:
        free.append((cur, duration))
    capacity = sum(int((e - s) // lo) for s, e in free)
    k = min(n, capacity)
    if k <= len(chosen):
        return chosen
    # distribute k slices over the free intervals in proportion to their length
    total = sum(e - s for s, e in free)
    slices = []
    for s, e in free:
        m = max(1, round(k * (e - s) / total)) if total else 1
        m = min(m, int((e - s) // lo))
        step = (e - s) / m if m else 0
        slices += [(s + i * step, s + (i + 1) * step) for i in range(m)]
    snap = (lambda t: min(bounds, key=lambda b: abs(b - t))) if bounds else (lambda t: t)
    result = []
    zmin = float(np.min(score)) if len(score) else 0.0
    for s, e in slices[:k]:
        # slices never overlap each other, so anything kept strictly inside a slice can't overlap either
        inside = [(score[i], c) for i, c in enumerate(cands) if c["start"] >= s - 1e-6 and c["end"] <= e + 1e-6]
        if inside:
            sc, c = max(inside, key=lambda x: x[0])
            c = dict(c, z=float(sc))
        else:
            a, b = snap(s), snap(e)  # prefer sentence/shot boundaries, but stay inside the slice
            a = a if s - 1e-6 <= a < e else s
            b = b if a < b <= e + 1e-6 else e
            if b - a < lo * 0.8:
                a, b = s, e
            c = {"start": a, "end": b, "text": "", "first": "", "z": zmin}
        if not _overlaps(c, result + taken):
            result.append(c)
    return result if len(result) > len(chosen) else chosen


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
