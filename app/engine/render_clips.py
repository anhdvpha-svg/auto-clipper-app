#!/usr/bin/env python3
"""Render short vertical clips (9:16) the way OpusClip does: per-shot framing, a hook title card,
word-highlighted captions, loudness-normalised audio, plus a report.

Usage:
  python render_clips.py --video VIDEO --clips work/clips.json --out-dir out/ \
      [--transcript work/transcript.json] [--shots work/shots.json]
Options:
  --shots FILE            shots.json from analyze_video.py -> automatic per-shot fill/fit framing,
                          black-bar removal. Without it every clip uses one framing (--reframe).
  --reframe auto|center|blur   default framing (auto needs --shots; default: auto if --shots else center)
  --no-captions           skip burned-in word captions
  --words-per-line N      max words shown at once (default 4)
  --highlight HEX         active-word color as RRGGBB (default FFE600, yellow)
  --uppercase             captions in UPPERCASE
  --no-hook               don't draw hook_title cards even if clips.json has them
  --no-loudnorm           keep original audio level (default: normalise to -14 LUFS, the social-media norm)
  --no-fade               no 0.3 s fade-out at the end
  --size WxH              output size (default 1080x1920)
  --max-fps N             cap frame rate (default 60; source fps is kept below it)
  --only 2,5              re-render only these clips (1-based)

clips.json format: see references/clips_schema.md.
"""
import argparse, json, os, re, shutil, subprocess, sys, unicodedata

PUNCT_END = re.compile(r"[.!?…:;]$")


def ffprobe_json(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout
    return json.loads(out)


def all_words(transcript):
    if not transcript:
        return []
    ws = []
    for seg in transcript["segments"]:
        ws.extend(seg.get("words") or [])
    return sorted(ws, key=lambda w: w["s"])


def snap_range(start, end, words, duration):
    """Move cut points to word boundaries so a clip never starts or ends mid-word."""
    # A word belongs to the clip only if it *starts* at/after the cut-in and *ends* by the cut-out
    # (small tolerance). Using "ends after start" instead would pull in the tail word of the previous
    # sentence, e.g. a clip opening on a stray "...thúc," - especially with SRT-estimated word timings.
    inside = [w for w in words if w["s"] >= start - 0.15 and w["e"] <= end + 0.15]
    if inside:
        start, end = inside[0]["s"] - 0.12, inside[-1]["e"] + 0.30
    start = max(0.0, start)
    if duration:
        end = min(duration, end)
    return round(start, 3), round(end, 3)


def ass_time(t):
    t = max(0.0, t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(s):
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def chunk_words(words, max_words, max_chars=22, max_gap=0.6):
    chunks, cur = [], []
    for w in words:
        if cur:
            text_len = sum(len(x["w"]) + 1 for x in cur) + len(w["w"])
            if (len(cur) >= max_words or text_len > max_chars or w["s"] - cur[-1]["e"] > max_gap
                    or PUNCT_END.search(cur[-1]["w"])):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words, W, H, max_words, highlight_rrggbb, uppercase):
    rr, gg, bb = highlight_rrggbb[0:2], highlight_rrggbb[2:4], highlight_rrggbb[4:6]
    hl = f"&H00{bb}{gg}{rr}&".upper()
    fs = int(W * 0.072)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Inter,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{max(3, fs // 12)},2,2,60,60,{int(H * 0.25)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    chunks = chunk_words(words, max_words)
    for ci, ch in enumerate(chunks):
        next_start = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else ch[-1]["e"] + 0.5
        texts = [ass_escape(w["w"].upper() if uppercase else w["w"]) for w in ch]
        for i, w in enumerate(ch):
            st = w["s"]
            en = ch[i + 1]["s"] if i + 1 < len(ch) else min(next_start, w["e"] + 0.4)
            if en <= st:
                en = st + 0.05
            parts = [(f"{{\\c{hl}}}{t}{{\\c&H00FFFFFF&}}" if j == i else t) for j, t in enumerate(texts)]
            pop = "{\\fscx108\\fscy108\\t(0,90,\\fscx100\\fscy100)}" if i == 0 else ""
            lines.append(f"Dialogue: 0,{ass_time(st)},{ass_time(en)},Cap,,0,0,0,,{pop}{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"


# ---------- hook title card (white rounded box, bold black uppercase, top of frame) ----------

FONTS_DIR = os.environ.get("AUTOCLIPPER_FONTS")  # the desktop app ships Inter here


def find_font(*names):
    if FONTS_DIR:
        p = os.path.join(FONTS_DIR, "Inter-ExtraBold.otf")
        if os.path.exists(p):
            return p
    for n in names:
        try:
            r = subprocess.run(["fc-match", "-f", "%{file}", n], capture_output=True, text=True, encoding="utf-8", errors="replace")
        except FileNotFoundError:  # Windows/macOS without fontconfig tools
            return None
        if r.returncode == 0 and r.stdout and os.path.exists(r.stdout):
            return r.stdout
    return None


def make_hook_png(text, W, path, uppercase=True):
    from PIL import Image, ImageDraw, ImageFont
    font_path = find_font("Inter:style=ExtraBold", "Montserrat:style=ExtraBold", "DejaVu Sans:style=Bold")
    fs = int(W * 0.06)  # matched to OpusClip: box ~3/4 of the frame width, 3-4 short lines
    font = ImageFont.truetype(font_path, fs) if font_path else ImageFont.load_default()
    text = text.upper() if uppercase else text
    max_w = int(W * 0.68)
    lines, cur = [], ""
    d = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    for word in text.split():
        trial = (cur + " " + word).strip()
        if cur and d.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    # no orphan: if the last line is a single word, pull one word down from the line above
    if len(lines) > 1 and len(lines[-1].split()) == 1 and len(lines[-2].split()) > 2:
        prev = lines[-2].split()
        lines[-2], lines[-1] = " ".join(prev[:-1]), prev[-1] + " " + lines[-1]
    lh = int(fs * 1.22)
    tw = max(int(d.textlength(l, font=font)) for l in lines)
    px, py = int(fs * 0.6), int(fs * 0.45)
    bw, bh = tw + 2 * px, lh * len(lines) + 2 * py
    img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, bw - 1, bh - 1], radius=int(fs * 0.3), fill=(255, 255, 255, 255))
    for i, l in enumerate(lines):
        lw = d.textlength(l, font=font)
        d.text(((bw - lw) / 2, py + i * lh + (lh - fs) * 0.35), l, font=font, fill=(0, 0, 0, 255))
    img.save(path)


# ---------- framing ----------

def layout_chain(k, lay, crop_x, act, W, H):
    """Filter text taking [s{k}] (already cut to the shot and to the active picture area) to [v{k}]."""
    aw, ah = act["w"], act["h"]
    src_ar, target_ar = aw / ah, W / H
    if lay == "fit" and src_ar > target_ar + 0.01:
        return (f"[s{k}]split[a{k}][b{k}];"
                f"[a{k}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"gblur=sigma=40,eq=brightness=-0.08[bg{k}];"
                f"[b{k}]scale={W}:-2[fg{k}];[bg{k}][fg{k}]overlay=(W-w)/2:(H-h)/2,setsar=1[v{k}]")
    if src_ar > target_ar:
        cw = int(ah * target_ar) // 2 * 2
        x = int(max(0, min(aw - cw, crop_x * aw - cw / 2)))
        return f"[s{k}]crop={cw}:{ah}:{x}:0,scale={W}:{H},setsar=1[v{k}]"
    return f"[s{k}]scale={W}:-2,crop={W}:{H},setsar=1[v{k}]"


def plan_segments(ranges, clip, default_mode, shots):
    """Split the clip's ranges at shot cuts and give each piece a layout and crop_x."""
    mode = clip.get("reframe", default_mode)
    overrides = clip.get("layout_overrides") or []
    segs = []
    for s, e in ranges:
        pieces = []
        if mode == "auto" and shots:
            for sh in shots:
                a, b = max(s, sh["start"]), min(e, sh["end"])
                if b - a > 0.04:
                    pieces.append([a, b, sh["layout"], sh["crop_x"], sh])
            if not pieces:
                pieces = [[s, e, "fill", 0.5, None]]
            # absorb slivers (<0.25 s) into the neighbour so the framing doesn't flicker
            merged = []
            for p in pieces:
                if merged and p[1] - p[0] < 0.25:
                    merged[-1][1] = p[1]
                else:
                    merged.append(p)
            if len(merged) > 1 and merged[0][1] - merged[0][0] < 0.25:  # leading sliver of the previous shot
                merged[1][0] = merged[0][0]
                merged.pop(0)
            pieces = merged
        else:
            lay = "fit" if mode == "blur" else "fill"
            pieces = [[s, e, lay, float(clip.get("crop_x", 0.5)), None]]
        for p in pieces:
            for o in overrides:  # {"at": source_seconds, "layout": "fit"|"fill", "crop_x": 0.4}
                if p[0] <= o["at"] < p[1] or (p[4] and p[4]["start"] <= o["at"] < p[4]["end"]):
                    p[2] = o.get("layout", p[2])
                    p[3] = float(o.get("crop_x", p[3]))
            if mode != "auto" and "crop_x" in clip:
                p[3] = float(clip["crop_x"])
            segs.append((p[0], p[1], p[2], p[3]))
    return segs


def slugify(text, maxlen=40):
    t = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:maxlen].rstrip("-") or "clip"


def render_clip(idx, clip, a, src, words, duration, shots, act):
    W, H = a.W, a.H
    raw_ranges = clip.get("ranges") or [[clip["start"], clip["end"]]]
    ranges = [snap_range(s, e, words, duration) for s, e in raw_ranges]
    seek = max(0.0, min(s for s, _ in ranges) - 1.0)  # input-seek near the clip, then trim precisely

    clip_words, offset = [], 0.0
    for s, e in ranges:
        for w in words:
            if w["s"] >= s - 0.05 and w["e"] <= e + 0.05:
                clip_words.append({"w": w["w"], "s": w["s"] - s + offset, "e": w["e"] - s + offset})
        offset += e - s
    total = offset

    segs = plan_segments(ranges, clip, a.reframe, shots)
    actcrop = f"crop={act['w']}:{act['h']}:{act['x']}:{act['y']}," if act["w"] != src["w"] or act["h"] != src["h"] else ""
    parts = []
    for k, (s, e, lay, cx) in enumerate(segs):
        parts.append(f"[0:v]trim=start={s - seek:.3f}:end={e - seek:.3f},setpts=PTS-STARTPTS,{actcrop}"
                     f"fps={a.fps},format=yuv420p[s{k}]")
        parts.append(layout_chain(k, lay, cx, act, W, H))
    parts.append("".join(f"[v{k}]" for k in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[vc]")
    last = "[vc]"

    slug = f"clip_{idx:02d}_{slugify(clip.get('title', ''))}"
    if not a.no_captions and clip_words:
        ass_name = f"{slug}.ass"
        with open(os.path.join(a.out_dir, ass_name), "w", encoding="utf-8") as f:
            f.write(build_ass(clip_words, W, H, a.words_per_line, a.highlight, a.uppercase))
        fontsdir = ""
        if FONTS_DIR and os.path.isdir(FONTS_DIR):
            # copy the fonts next to the .ass so the filter path needs no escaping (Windows "C:" etc.)
            local = os.path.join(a.out_dir, "_fonts")
            os.makedirs(local, exist_ok=True)
            for fn in os.listdir(FONTS_DIR):
                if fn.lower().endswith((".otf", ".ttf")) and not os.path.exists(os.path.join(local, fn)):
                    shutil.copy(os.path.join(FONTS_DIR, fn), local)
            fontsdir = ":fontsdir=_fonts"
        parts.append(f"{last}ass={ass_name}{fontsdir}[vcap]")
        last = "[vcap]"

    extra_inputs = []
    hook = clip.get("hook_title")
    if hook and not a.no_hook:
        hs = min(float(clip.get("hook_seconds", 4.0)), total)
        png = f"{slug}_hook.png"
        make_hook_png(hook, W, os.path.join(a.out_dir, png))
        extra_inputs = ["-loop", "1", "-t", f"{hs:.2f}", "-i", png]
        parts.append(f"[1:v]format=rgba,fade=in:st=0:d=0.25:alpha=1,"
                     f"fade=out:st={max(0, hs - 0.3):.2f}:d=0.3:alpha=1[ttl]")
        parts.append(f"{last}[ttl]overlay=(W-w)/2:{int(H * 0.04)}:eof_action=pass[vh]")
        last = "[vh]"

    if not a.no_fade and total > 2:
        parts.append(f"{last}fade=out:st={total - 0.3:.3f}:d=0.3[vf]")
        last = "[vf]"

    amap = []
    if src["has_audio"]:
        for k, (s, e) in enumerate(ranges):
            parts.append(f"[0:a]atrim=start={s - seek:.3f}:end={e - seek:.3f},asetpts=PTS-STARTPTS[ar{k}]")
        chain = "".join(f"[ar{k}]" for k in range(len(ranges))) + f"concat=n={len(ranges)}:v=0:a=1"
        if not a.no_loudnorm:
            chain += ",loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
        if not a.no_fade and total > 2:
            chain += f",afade=t=out:st={total - 0.3:.3f}:d=0.3"
        parts.append(chain + "[aout]")
        amap = ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]

    out_name = f"{slug}.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{seek:.3f}", "-i", os.path.abspath(a.video), *extra_inputs,
           "-filter_complex", ";".join(parts), "-map", last, *amap,
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", "-t", f"{total:.3f}", out_name]
    lays = "".join("F" if l == "fill" else "B" for _, _, l, _ in segs)
    print(f"[{slug}] {total:.1f}s ranges={ranges} shots={lays} (F=fill, B=fit/blur)", file=sys.stderr)
    subprocess.run(cmd, check=True, cwd=a.out_dir)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{min(1.0, total / 2):.2f}", "-i", out_name,
                    "-frames:v", "1", "-vf", "scale=360:-2", f"{slug}.jpg"], cwd=a.out_dir)
    return {"file": out_name, "thumb": f"{slug}.jpg", "ranges": ranges, "length": round(total, 1),
            "words": len(clip_words), "shot_layouts": lays}


def fmt(t):
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:04.1f}"


def write_report(path, clips, results, source):
    L = ["# Clip report", "", f"Source: `{os.path.basename(source)}` · {len(clips)} clips", "",
         "| # | Title | Score | Length | Time in source | File |", "|---|---|---|---|---|---|"]
    order = sorted(range(len(clips)), key=lambda i: -(clips[i].get("score", {}) or {}).get("total", 0))
    for i in order:
        c, r = clips[i], results[i]
        span = ", ".join(f"{fmt(s)}–{fmt(e)}" for s, e in r["ranges"])
        L.append(f"| {i + 1} | {c.get('title', '')} | {(c.get('score') or {}).get('total', '–')} | "
                 f"{r['length']}s | {span} | `{r['file']}` |")
    for i in order:
        c, r = clips[i], results[i]
        sc = c.get("score") or {}
        L += ["", f"## {i + 1}. {c.get('title', '')}", "", f"![thumb]({r['thumb']})", "",
              f"- **File:** `{r['file']}` ({r['length']}s)"]
        if c.get("hook_title"):
            L.append(f"- **On-screen hook:** {c['hook_title']}")
        if sc:
            parts = ", ".join(f"{k} {v}" for k, v in sc.items() if k != "total")
            L.append(f"- **Virality score:** {sc.get('total', '–')}/100 ({parts})")
        if c.get("hook"):
            L.append(f"- **Opening line:** {c['hook']}")
        if c.get("reason"):
            L.append(f"- **Why it works:** {c['reason']}")
        for plat, text in (c.get("captions") or {}).items():
            L += ["", f"**{plat}:**", "", "```", text, "```"]
        if c.get("hashtags"):
            L += ["", "**Hashtags:** " + " ".join(c["hashtags"])]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--transcript")
    ap.add_argument("--clips", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--shots")
    ap.add_argument("--reframe", choices=["auto", "center", "blur"])
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--words-per-line", type=int, default=4)
    ap.add_argument("--highlight", default="FFE600")
    ap.add_argument("--uppercase", action="store_true")
    ap.add_argument("--no-hook", action="store_true")
    ap.add_argument("--no-loudnorm", action="store_true")
    ap.add_argument("--no-fade", action="store_true")
    ap.add_argument("--size", default="1080x1920")
    ap.add_argument("--max-fps", type=float, default=60)
    ap.add_argument("--only", help="comma-separated 1-based clip numbers to (re)render")
    a = ap.parse_args(argv)
    a.W, a.H = (int(x) for x in a.size.lower().split("x"))
    a.highlight = a.highlight.lstrip("#")
    os.makedirs(a.out_dir, exist_ok=True)

    transcript = json.load(open(a.transcript, encoding="utf-8")) if a.transcript else None
    spec = json.load(open(a.clips, encoding="utf-8"))
    clips = spec["clips"] if isinstance(spec, dict) else spec
    info = ffprobe_json(a.video)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    src = {"w": int(v["width"]), "h": int(v["height"]),
           "has_audio": any(s["codec_type"] == "audio" for s in info["streams"])}
    if str((v.get("tags") or {}).get("rotate", "0")) in ("90", "270", "-90"):
        src["w"], src["h"] = src["h"], src["w"]
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").split("/")
    src_fps = float(num) / float(den) if float(den) else 30.0
    a.fps = round(min(src_fps if src_fps > 1 else 30.0, a.max_fps), 3)
    duration = float(info["format"].get("duration", 0)) or (transcript or {}).get("duration")
    words = all_words(transcript)

    shots, act = None, {"x": 0, "y": 0, "w": src["w"], "h": src["h"]}
    if a.shots:
        sj = json.load(open(a.shots))
        shots = sj["shots"]
        act = sj.get("active_area") or act
    a.reframe = a.reframe or ("auto" if shots else "center")
    if a.reframe == "auto" and not shots:
        sys.exit("--reframe auto needs --shots shots.json (run analyze_video.py first)")

    only = {int(x) for x in a.only.split(",")} if a.only else None
    prev = {}
    prev_path = os.path.join(a.out_dir, "clips_rendered.json")
    if only and os.path.exists(prev_path):
        prev = {i: r for i, r in enumerate(json.load(open(prev_path, encoding="utf-8"))["rendered"], 1)}
    results = []
    for i, c in enumerate(clips, 1):
        if only and i not in only and i in prev:
            results.append(prev[i])
            continue
        results.append(render_clip(i, c, a, src, words, duration, shots, act))
    write_report(os.path.join(a.out_dir, "report.md"), clips, results, a.video)
    json.dump({"clips": clips, "rendered": results}, open(prev_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"Done: {len(results)} clips + report.md in {a.out_dir} ({a.fps:g} fps)")


if __name__ == "__main__":
    main()
