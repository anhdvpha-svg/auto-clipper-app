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

try:  # imported as part of the desktop app (package) or run as a script next to beats.py
    from . import beats
except ImportError:
    import beats

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


# ---------- caption typography presets (trendy short-video styles) ----------
# font = family name inside the bundled font files; bord = outline as a fraction of the font size;
# mode: color (active word recoloured) | box (active word on a coloured pill) | size (active word bigger)
#       | karaoke (colour sweeps through the words) ; anim: pop (line pops in) | bounce (each word) | none
CAPTION_PRESETS = {
    "classic":  {"label": "Cổ điển vàng", "font": "Inter", "bold": 1, "color": "FFFFFF", "hl": "FFE600",
                 "outline": "000000", "bord": 0.085, "shadow": 2, "upper": True, "mode": "color", "anim": "pop"},
    "hormozi":  {"label": "Hormozi", "font": "Anton", "color": "FFFFFF", "hl": "FFE600", "hl2": "39E75F",
                 "outline": "000000", "bord": 0.12, "shadow": 3, "upper": True, "mode": "color", "anim": "pop",
                 "size": 1.2, "words": 3},
    "beast":    {"label": "MrBeast", "font": "Bangers", "color": "FFFFFF", "hl": "FFE600", "hl2": "FF3355",
                 "outline": "000000", "bord": 0.14, "shadow": 5, "upper": True, "mode": "color", "anim": "bounce",
                 "size": 1.28, "spacing": 2, "words": 3},
    "box":      {"label": "Hộp màu", "font": "Be Vietnam Pro ExtraBold", "color": "FFFFFF", "hl": "8B5CF6",
                 "ink": "FFFFFF", "outline": "000000", "bord": 0.07, "shadow": 1, "upper": False, "mode": "box",
                 "anim": "pop", "size": 0.98},
    "neon":     {"label": "Neon", "font": "Paytone One", "color": "FFFFFF", "hl": "00F0FF", "outline": "FF00C8",
                 "bord": 0.05, "shadow": 0, "blur": 4, "upper": True, "mode": "color", "anim": "pop", "size": 1.05},
    "karaoke":  {"label": "Karaoke", "font": "Be Vietnam Pro Black", "color": "FFFFFF", "hl": "FFE600",
                 "outline": "000000", "bord": 0.09, "shadow": 2, "upper": True, "mode": "karaoke", "anim": "none",
                 "size": 1.0, "words": 5},
    "oneword":  {"label": "Từng chữ", "font": "Anton", "color": "FFFFFF", "hl": "FFE600", "outline": "000000",
                 "bord": 0.12, "shadow": 4, "upper": True, "mode": "color", "anim": "bounce", "size": 1.6, "words": 1},
    "minimal":  {"label": "Tối giản", "font": "Be Vietnam Pro ExtraBold", "color": "FFFFFF", "hl": "FFFFFF",
                 "outline": "000000", "bord": 0.0, "shadow": 3, "upper": False, "mode": "size", "anim": "none",
                 "size": 0.86, "words": 6},
    "subtitle": {"label": "Nền đen", "font": "Inter", "bold": 1, "color": "FFFFFF", "hl": "FFE600",
                 "outline": "000000", "back": "000000", "boxed": True, "bord": 0.22, "shadow": 0, "upper": False,
                 "mode": "color", "anim": "none", "size": 0.86, "words": 6},
    "handwrite": {"label": "Viết tay", "font": "Patrick Hand", "color": "FFFFFF", "hl": "FFB020",
                  "outline": "1A1A1A", "bord": 0.09, "shadow": 2, "upper": False, "mode": "color", "anim": "bounce",
                  "size": 1.18},
}

# fonts the hook card can use: label -> file in the bundled fonts folder
HOOK_FONTS = {"Inter": "Inter-ExtraBold.otf", "Anton": "Anton-Regular.ttf", "Bangers": "Bangers-Regular.ttf",
              "Be Vietnam Pro": "BeVietnamPro-Black.ttf", "Paytone One": "PaytoneOne-Regular.ttf",
              "Patrick Hand": "PatrickHand-Regular.ttf"}

_EMPH = re.compile(r"^[\W_]*(\$?\d[\d.,:%]*\w{0,3}%?|[A-ZĐ][A-ZĐ0-9]{2,})[\W_]*$")
POWER_WORDS = {
    # vi (single syllables, so only ones that rarely appear outside a strong phrase)
    "tiền", "triệu", "tỷ", "miễn", "phí", "bí", "mật", "sốc", "giàu", "nghèo", "đỉnh", "khủng",
    # en
    "money", "million", "billion", "free", "secret", "never", "always", "best", "worst", "stop", "mistake",
    "insane", "crazy", "huge", "biggest", "truth", "rich", "broke",
}


def is_emphasis(word):
    """Numbers, money, ALL-CAPS and a short list of power words get a second colour: the eye lands on them."""
    w = word.strip()
    if _EMPH.match(w):
        return True
    return re.sub(r"[^\w]", "", w.lower()) in POWER_WORDS


def _ass_col(rrggbb, alpha=0):
    rrggbb = (rrggbb or "FFFFFF").lstrip("#")
    return f"&H{alpha:02X}{rrggbb[4:6]}{rrggbb[2:4]}{rrggbb[0:2]}&".upper()


def caption_preset(name):
    p = dict(CAPTION_PRESETS["classic"])
    own = CAPTION_PRESETS.get(name or "classic", {})
    if "bold" not in own:
        p.pop("bold", None)  # single-weight display fonts: no faux bold
    p.update(own)
    return p


def build_ass(words, W, H, max_words, highlight_rrggbb, uppercase, size=1.0, y=0.75, chunks=None,
              preset="classic", emphasis=False):
    """Word-highlighted captions in one of the CAPTION_PRESETS. `chunks` (edited by the user: [{"s","e","text"}]
    in clip time) replace the automatic grouping; their words get times spread over the chunk by word length.
    highlight_rrggbb / uppercase None -> the preset's own."""
    P = caption_preset(preset)
    hl_hex = (highlight_rrggbb or P["hl"]).lstrip("#")
    hl, base = _ass_col(hl_hex), _ass_col(P["color"])
    hl2 = _ass_col(P.get("hl2") or ("FFE600" if hl_hex.upper() != "FFE600" else "39E75F"))
    upper = P["upper"] if uppercase is None else uppercase
    fs = int(W * 0.072 * size * P.get("size", 1.0))
    bord = max(0, round(fs * P["bord"])) if not P.get("boxed") else max(6, round(fs * P["bord"]))
    margin_v = int(H * (1 - y))  # y = vertical position of the caption baseline (0 top .. 1 bottom)
    style_border = 3 if P.get("boxed") else 1
    outline_col = _ass_col(P.get("back") or P["outline"])  # boxed: opaque, or overlapping glyph boxes show seams
    primary, secondary = (hl, base) if P["mode"] == "karaoke" else (base, base)
    sub_fs = max(24, int(W * 0.036 * size))           # second-language line under the captions (bilingual)
    sub_margin = max(20, margin_v - int(sub_fs * 1.5))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{P["font"]},{fs},{primary},{secondary},{outline_col},&H80000000,{-1 if P.get("bold") else 0},0,0,0,100,100,{P.get("spacing", 0)},0,{style_border},{bord},{P["shadow"]},2,60,60,{margin_v},1
Style: Sub,Inter,{sub_fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{max(2, sub_fs // 10)},1,2,90,90,{sub_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    max_words = P.get("words", max_words) if max_words == 4 else max_words
    max_chars = 22 if max_words <= 4 else 34
    subs = []  # (start, end, text) of second-language lines
    if chunks is not None:
        groups = []
        for c in chunks:
            toks = (c.get("text") or "").split()
            if c.get("sub") and c["e"] > c["s"]:
                subs.append((c["s"], c["e"], c["sub"]))
            if not toks or c["e"] <= c["s"]:
                continue
            total = sum(len(t) + 1 for t in toks)
            cur, g = c["s"], []
            for t in toks:
                d = (c["e"] - c["s"]) * (len(t) + 1) / total
                g.append({"w": t, "s": cur, "e": cur + d})
                cur += d
            # long lines (translations, edits) are shown a few words at a time, like the automatic captions
            groups += chunk_words(g, max_words, max_chars=max_chars, max_gap=99)
    else:
        groups = chunk_words(words, max_words, max_chars=max_chars)
    blur = f"{{\\blur{P['blur']}}}" if P.get("blur") else ""
    ink = _ass_col(P.get("ink", "000000"))
    lines = []
    for ci, ch in enumerate(groups):
        next_start = groups[ci + 1][0]["s"] if ci + 1 < len(groups) else ch[-1]["e"] + 0.5
        texts = [ass_escape(w["w"].upper() if upper else w["w"]) for w in ch]
        emph = [emphasis and is_emphasis(w["w"]) for w in ch]
        line_end = min(next_start, ch[-1]["e"] + 0.4)
        if P["mode"] == "karaoke":
            ks = []
            for i, w in enumerate(ch):
                nxt = ch[i + 1]["s"] if i + 1 < len(ch) else w["e"]
                ks.append(f"{{\\kf{max(1, int(round((nxt - w['s']) * 100)))}}}{texts[i]}")
            lines.append(f"Dialogue: 0,{ass_time(ch[0]['s'])},{ass_time(line_end)},Cap,,0,0,0,,{blur}" + " ".join(ks))
            continue
        for i, w in enumerate(ch):
            st = w["s"]
            en = ch[i + 1]["s"] if i + 1 < len(ch) else line_end
            if en <= st:
                en = st + 0.05
            parts = []
            for j, t in enumerate(texts):
                rest = f"{{\\c{base}}}"
                if j == i:
                    if P["mode"] == "box":
                        on = f"{{\\c{ink}\\3c{hl}\\bord{max(8, fs // 5)}\\shad0}}"
                        rest = f"{{\\c{base}\\3c{outline_col}\\bord{bord}\\shad{P['shadow']}}}"
                    elif P["mode"] == "size":
                        on = "{\\fscx112\\fscy112}"
                        rest = "{\\fscx100\\fscy100}"
                    else:
                        on = f"{{\\c{hl}}}"
                    if P["anim"] == "bounce":
                        on = on[:-1] + "\\fscx125\\fscy125\\t(0,110,\\fscx100\\fscy100)}" if P["mode"] != "size" else \
                            "{\\fscx125\\fscy125\\t(0,110,\\fscx112\\fscy112)}"
                        rest = rest[:-1] + "\\fscx100\\fscy100}"
                    parts.append(f"{on}{t}{rest}")
                elif emph[j]:
                    parts.append(f"{{\\c{hl2}}}{t}{{\\c{base}}}")
                else:
                    parts.append(t)
            pop = "{\\fscx86\\fscy86\\t(0,90,\\fscx100\\fscy100)}" if i == 0 and P["anim"] == "pop" else ""
            lines.append(f"Dialogue: 0,{ass_time(st)},{ass_time(en)},Cap,,0,0,0,,{blur}{pop}{' '.join(parts)}")
    for a0, b0, t in subs:
        lines.append(f"Dialogue: 1,{ass_time(a0)},{ass_time(b0)},Sub,,0,0,0,,{{\\q0}}{ass_escape(t)}")
    return header + "\n".join(lines) + "\n"


def caption_chunks(clip_words, max_words=4):
    """The automatic caption lines for a clip, as editable chunks [{"s","e","text"}] (clip time)."""
    out = []
    for ch in chunk_words(clip_words, max_words):
        out.append({"s": round(ch[0]["s"], 3), "e": round(ch[-1]["e"], 3), "text": " ".join(w["w"] for w in ch)})
    return out


# ---------- hook title card (OpusClip style: white rounded box, bold black caps, top of frame) ----------

def fonts_dir():
    return os.environ.get("AUTOCLIPPER_FONTS")  # read at call time: the desktop app sets it after import


def find_font(*names, font="Inter"):
    fd = fonts_dir()
    if fd:
        for fn in (HOOK_FONTS.get(font, ""), "Inter-ExtraBold.otf"):
            p = os.path.join(fd, fn) if fn else ""
            if p and os.path.exists(p):
                return p
    for n in names:
        try:
            r = subprocess.run(["fc-match", "-f", "%{file}", n], capture_output=True, text=True, encoding="utf-8", errors="replace")
        except FileNotFoundError:  # Windows/macOS without fontconfig tools
            break
        if r.returncode == 0 and r.stdout and os.path.exists(r.stdout):
            return r.stdout
    for p in (r"C:\Windows\Fonts\seguibl.ttf", r"C:\Windows\Fonts\arialbd.ttf",  # Windows fallbacks
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf"):
        if os.path.exists(p):
            return p
    return None


# box colour, text colour, text outline colour (None = no outline)
HOOK_THEMES = {"light": ((255, 255, 255, 255), (0, 0, 0, 255), None),    # white box, black text (OpusClip default)
               "dark": ((0, 0, 0, 235), (255, 255, 255, 255), None),
               "accent": (None, (0, 0, 0, 255), None),                  # box in the highlight colour
               "red": ((230, 33, 41, 255), (255, 255, 255, 255), None),  # YouTube-red banner
               "outline": ((0, 0, 0, 0), (255, 255, 255, 255), (0, 0, 0, 255)),   # no box, white with black stroke
               "yellow": ((0, 0, 0, 0), (255, 230, 0, 255), (0, 0, 0, 255)),      # no box, yellow with black stroke
               "glass": ((20, 20, 20, 150), (255, 255, 255, 255), None)}           # translucent dark box

HOOK_THEME_LABELS = {"light": "Hộp trắng", "dark": "Hộp đen", "accent": "Hộp màu nhấn", "red": "Băng đỏ",
                     "outline": "Chữ viền đen", "yellow": "Chữ vàng viền", "glass": "Kính mờ"}


def hook_y(style, box_h, H):
    """Top of the hook card: a dragged position (fraction of the height) wins over the named spots."""
    if style.get("y") is not None:
        return int(max(0, min(H - box_h, float(style["y"]) * H)))
    pos = style.get("position", "top")
    return {"top": int(H * 0.06), "middle": int((H - box_h) / 2), "bottom": int(H * 0.60 - box_h)}.get(pos, int(H * 0.06))


def make_hook_png(text, W, path, uppercase=True, size=1.0, theme="light", accent="FFE600", font="Inter"):
    """Line breaks typed by the user are kept; long lines are wrapped to ~3/4 of the frame width."""
    from PIL import Image, ImageDraw, ImageFont
    font_path = find_font("Inter:style=ExtraBold", "Montserrat:style=ExtraBold", "DejaVu Sans:style=Bold", font=font)
    scale = {"Anton": 1.12, "Bangers": 1.18, "Patrick Hand": 1.15}.get(font, 1.0)
    fs = max(20, int(W * 0.062 * size * scale))
    if font_path:
        font = ImageFont.truetype(font_path, fs)
    else:
        font = ImageFont.load_default(size=fs)  # scalable built-in font (Pillow >= 10.1)
    text = text.upper() if uppercase else text
    max_w = int(W * 0.70)
    d = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lines = []
    for para in [p for p in text.replace("\\n", "\n").split("\n") if p.strip()]:
        cur, para_lines = "", []
        for word in para.split():
            trial = (cur + " " + word).strip()
            if cur and d.textlength(trial, font=font) > max_w:
                para_lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            para_lines.append(cur)
        # no orphan: if the last line is a single word, pull one word down from the line above
        if len(para_lines) > 1 and len(para_lines[-1].split()) == 1 and len(para_lines[-2].split()) > 2:
            prev = para_lines[-2].split()
            para_lines[-2], para_lines[-1] = " ".join(prev[:-1]), prev[-1] + " " + para_lines[-1]
        lines += para_lines
    if not lines:
        return False
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * 1.08)
    tw = max(int(d.textlength(l, font=font)) for l in lines)
    px, py = int(fs * 0.62), int(fs * 0.42)
    bw, bh = tw + 2 * px, lh * len(lines) + 2 * py
    box, ink, stroke = HOOK_THEMES.get(theme, HOOK_THEMES["light"])
    if box is None:
        box = tuple(int(accent[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
    sw = max(3, fs // 9) if stroke else 0
    img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if box[3]:
        d.rounded_rectangle([0, 0, bw - 1, bh - 1], radius=int(fs * 0.32), fill=box)
    for i, l in enumerate(lines):
        lw = d.textlength(l, font=font)
        d.text(((bw - lw) / 2, py + i * lh), l, font=font, fill=ink, stroke_width=sw, stroke_fill=stroke)
    img.save(path)
    return True


# ---------- framing ----------

def plan_segments(ranges, clip, default_mode, shots):
    """Split the clip's ranges at shot cuts and give each piece a layout and a crop:
    (start, end, layout, crop_x, extra) with extra = {"zoom", "crop_y", "track"} for face punch-ins and pans.
    Pieces always cover each range end to end (gaps take the neighbouring shot's framing)."""
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
            pieces.sort(key=lambda p: p[0])
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
            pieces[0][0], pieces[-1][1] = s, e
            for prev, nxt in zip(pieces, pieces[1:]):
                prev[1] = nxt[0]
        else:
            lay = "fit" if mode == "blur" else "fill"
            pieces = [[s, e, lay, float(clip.get("crop_x", 0.5)), None]]
        for p in pieces:
            sh = p[4] or {}
            extra = {"zoom": float(sh.get("zoom") or 1.0), "crop_y": float(sh.get("crop_y", 0.5)),
                     "track": sh.get("track")}
            for o in overrides:  # {"at": source_seconds, "layout": "fit"|"fill", "crop_x": 0.4, "zoom": 1.2}
                if p[0] <= o["at"] < p[1] or (p[4] and p[4]["start"] <= o["at"] < p[4]["end"]):
                    p[2] = o.get("layout", p[2])
                    if "crop_x" in o:
                        p[3] = float(o["crop_x"])
                        extra["track"] = None  # placed by hand: hold still
                    if "zoom" in o:
                        extra["zoom"] = float(o["zoom"])
                    if "crop_y" in o:
                        extra["crop_y"] = float(o["crop_y"])
            if mode != "auto" and "crop_x" in clip:
                p[3] = float(clip["crop_x"])
            segs.append((p[0], p[1], p[2], p[3], extra))
    return segs


def crop_geometry(aw, ah, W, H, zoom=1.0):
    """Size of the source window that fills the W x H output at this zoom."""
    target_ar = W / H
    z = max(1.0, zoom or 1.0)
    cw = int(ah * target_ar / z) // 2 * 2
    ch = int(ah / z) // 2 * 2
    return cw, ch


def crop_xy(cx, cy, cw, ch, aw, ah):
    """Top-left of the crop window for a subject at (cx, cy) (fractions): centred across, and the face a bit
    above the middle (40% from the top) when zoomed in, the way a camera operator frames a close-up."""
    x = int(max(0, min(aw - cw, cx * aw - cw / 2)))
    y = int(max(0, min(ah - ch, cy * ah - ch * 0.4))) if ch < ah else 0
    return x, y


def _piecewise(items, default):
    """items [(a, b, expr)] in stream time -> one ffmpeg expression."""
    out = str(default)
    for a0, b0, ex in reversed(items):
        out = f"if(between(t,{a0:.3f},{b0:.3f}),{ex},{out})"
    return out


def _x_expr(piece, cw, ch, aw, ah, rs):
    """Crop x for one piece: fixed, or a smooth pan along the tracked face keyframes."""
    a0, b0, _, cx, ex = piece
    x0, _ = crop_xy(cx, ex.get("crop_y", 0.5), cw, ch, aw, ah)
    tr = ex.get("track")
    if not tr:
        return str(x0)
    pts = [(t - rs, crop_xy(x, 0.5, cw, ch, aw, ah)[0]) for t, x in tr]
    pts = [(max(a0, min(b0, t)), x) for t, x in pts]
    pts.sort()
    expr = str(pts[-1][1])
    for (t1, x1), (t2, x2) in reversed(list(zip(pts, pts[1:]))):
        if t2 - t1 < 0.05:
            continue
        # ease in-out between keyframes: no jerky linear starts
        k = f"(1-cos(PI*(t-{t1:.3f})/{t2 - t1:.3f}))/2"
        expr = f"if(lt(t,{t2:.3f}),{x1}+({x2 - x1})*{k},{expr})"
    return f"if(lt(t,{pts[0][0]:.3f}),{pts[0][1]},{expr})"


def range_chain(k, s, e, pieces, seek, actcrop, act, W, H, fps):
    """One continuous stream per range; the framing switches at shot cuts inside the stream
    (time-based crop + overlay enable), so there are no joins to stutter. Fill pieces are grouped by zoom
    level (crop size is fixed per filter), each group is one crop branch with per-time x/y."""
    aw, ah = act["w"], act["h"]
    src_ar, target_ar = aw / ah, W / H
    head = f"[0:v]trim=start={s - seek:.3f}:end={e - seek:.3f},setpts=PTS-STARTPTS,{actcrop}fps={fps},format=yuv420p"
    rel = [(ps - s, pe - s, lay, cx, ex) for ps, pe, lay, cx, ex in pieces]
    if src_ar <= target_ar + 0.01:  # already vertical: just fill
        return f"{head},scale={W}:-2,crop={W}:{H},setsar=1[v{k}]"

    fills = [r for r in rel if r[2] != "fit"]
    fits = [r for r in rel if r[2] == "fit"]
    groups = {}
    for r in fills:
        z = round(max(1.0, r[4].get("zoom", 1.0)), 2) if src_ar > target_ar * 1.2 else 1.0
        groups.setdefault(z, []).append(r)
    branches = []  # (filter chain, pieces it shows)
    for z, rs_ in sorted(groups.items()):
        cw, ch = crop_geometry(aw, ah, W, H, z)
        xs = [(a0, b0, _x_expr(r, cw, ch, aw, ah, s)) for r in rs_ for a0, b0 in [(r[0], r[1])]]
        ys = [(r[0], r[1], str(crop_xy(r[3], r[4].get("crop_y", 0.5), cw, ch, aw, ah)[1])) for r in rs_]
        xexpr = xs[0][2] if len({x[2] for x in xs}) == 1 else _piecewise(xs[:-1], xs[-1][2])
        yexpr = ys[0][2] if len({y[2] for y in ys}) == 1 else _piecewise(ys[:-1], ys[-1][2])
        q = lambda ex: ex if ex.lstrip("-").isdigit() else f"'{ex}'"
        branches.append((f"crop={cw}:{ch}:{q(xexpr)}:{q(yexpr)},scale={W}:{H},setsar=1", rs_))
    bw, bh = W // 4 // 2 * 2, H // 4 // 2 * 2
    fit_bg = (f"scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=8:2,"
              f"scale={W}:{H}:flags=bilinear,eq=brightness=-0.08")
    if fits:
        branches.insert(0, (f"split[fa{k}][fb{k}];[fa{k}]{fit_bg}[bg{k}];[fb{k}]scale={W}:-2[fg{k}];"
                            f"[bg{k}][fg{k}]overlay=(W-w)/2:(H-h)/2,setsar=1", fits))
    if len(branches) == 1:
        return f"{head},{branches[0][0]}[v{k}]"
    n = len(branches)
    out = f"{head},split={n}" + "".join(f"[s{k}_{i}]" for i in range(n)) + ";"
    out += ";".join(f"[s{k}_{i}]{chain}[b{k}_{i}]" for i, (chain, _) in enumerate(branches)) + ";"
    last = f"[b{k}_0]"
    for i in range(1, n):
        enable = "+".join(f"between(t,{a0:.3f},{b0 - 0.001:.3f})" for a0, b0, *_ in branches[i][1])
        nxt = f"[v{k}]" if i == n - 1 else f"[o{k}_{i}]"
        out += f"{last}[b{k}_{i}]overlay=0:0:enable='{enable}'{nxt}" + (";" if i < n - 1 else "")
        last = nxt
    return out


def slugify(text, maxlen=40):
    t = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:maxlen].rstrip("-") or "clip"


def clip_ranges(clip, words, duration):
    raw = clip.get("ranges") or [[clip["start"], clip["end"]]]
    return [snap_range(s, e, words, duration) for s, e in raw]


def clip_words_of(ranges, words):
    out, offset = [], 0.0
    for s, e in ranges:
        for w in words:
            if w["s"] >= s - 0.05 and w["e"] <= e + 0.05:
                out.append({"w": w["w"], "s": w["s"] - s + offset, "e": w["e"] - s + offset})
        offset += e - s
    return out, offset


def measure_loudness(video, ranges):
    """Integrated loudness (LUFS) and true peak (dBTP) of the clip's span, measured only.
    The loudnorm *filter* is not used on the output: it resamples to 192 kHz internally and at the start of a
    clip it can emit audio frames with duplicated/jumping timestamps - players then drop the first seconds
    of sound. A plain measured gain (volume) never touches timestamps."""
    s, e = min(r[0] for r in ranges), max(r[1] for r in ranges)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", video,
                        "-vn", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
    if not m:
        return None
    try:
        j = json.loads(m[-1])
        i, tp = float(j["input_i"]), float(j["input_tp"])
    except (KeyError, ValueError):
        return None
    if i < -70:  # silence: nothing to normalise
        return None
    return {"i": i, "tp": tp}


def audio_filter(m, audio_opts, total, fade):
    """Gain to -14 LUFS (+ user gain), a true-peak limiter only when needed, then clean timestamps."""
    opts = audio_opts or {}
    gain = 0.0
    if m and opts.get("normalize", True):
        gain = max(-20.0, min(20.0, -14.0 - m["i"]))
    gain += float(opts.get("gain_db", 0.0))
    f = []
    if abs(gain) > 0.05:
        f.append(f"volume={gain:.2f}dB")
    peak = (m["tp"] if m else -1.0) + gain
    if peak > -1.0:
        f.append("alimiter=limit=0.84:attack=5:release=50:level=disabled")  # 0.84 ~ -1.5 dBFS
    f += ["aresample=48000", "asetpts=N/SR/TB", "afade=t=in:d=0.02"]
    if fade and total > 2:
        f.append(f"afade=t=out:st={total - 0.3:.3f}:d=0.3")
    return ",".join(f)


# ---------- encoding ----------

ENCODERS = {
    # name: (ffmpeg args, pixel format)
    "nvenc": (["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "21", "-b:v", "0",
               "-maxrate", "25M", "-bufsize", "50M", "-profile:v", "high"], "yuv420p"),
    "qsv": (["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "21", "-profile:v", "high"], "nv12"),
    "amf": (["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22",
             "-profile:v", "high"], "nv12"),
    "x264": (["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-profile:v", "high"], "yuv420p"),
}
_ENC_CACHE = {}


def pick_encoder(choice="auto"):
    """'auto' -> first hardware H.264 encoder that actually works on this PC (NVIDIA, Intel, AMD), else x264."""
    if choice != "auto":
        return choice if choice in ENCODERS else "x264"
    if "auto" in _ENC_CACHE:
        return _ENC_CACHE["auto"]
    found = "x264"
    for name in ("nvenc", "qsv", "amf"):
        args, pix = ENCODERS[name]
        r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                            "color=black:s=1080x1920:r=30:d=0.3", "-pix_fmt", pix, *args, "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            found = name
            break
    _ENC_CACHE["auto"] = found
    return found


FILLERS = {"ừ", "ừm", "ờ", "à", "ầ", "ơ", "ư", "hmm", "um", "uh", "uhm", "erm", "ah", "eh", "mm"}


def jumpcut_ranges(ranges, words, min_gap=0.42, pad=0.09, min_piece=0.7, max_cuts=30):
    """Jump cuts: drop the dead air between phrases and filler sounds ("ừm", "uh"...), the fast-paced
    editing that keeps viewers on short-video platforms. Works on word timestamps, so cuts land between words."""
    out = []
    for s, e in ranges:
        ws = [w for w in words if w["s"] >= s - 0.05 and w["e"] <= e + 0.05]
        keep = [w for w in ws if re.sub(r"[^\w]", "", w["w"].lower()) not in FILLERS or w["e"] - w["s"] > 0.9]
        if len(keep) < 2:
            out.append([s, e])
            continue
        gaps = []
        for w1, w2 in zip(keep, keep[1:]):
            g = w2["s"] - w1["e"]
            if g > min_gap:
                gaps.append((g, w1["e"] + pad, w2["s"] - pad))
        gaps = sorted(sorted(gaps, reverse=True)[:max_cuts], key=lambda x: x[1])
        cur = s
        for _, a0, b0 in gaps:
            if b0 - a0 < 0.15 or a0 - cur < min_piece:
                continue
            out.append([cur, a0])
            cur = b0
        if out and e - cur < min_piece and out[-1][0] >= s:  # tiny tail: join to the previous piece
            cur = out.pop()[0]
        out.append([cur, e])
    return [[round(x, 3), round(y, 3)] for x, y in out]


def render_clip(idx, clip, a, src, words, duration, shots, act):
    W, H = a.W, a.H
    ranges = clip_ranges(clip, words, duration)
    base_ranges = ranges
    jumpcut = clip.get("jumpcut", a.jumpcut)
    if jumpcut and words:
        ranges = jumpcut_ranges(ranges, words)
    seek = max(0.0, min(s for s, _ in ranges) - 1.0)  # input-seek near the clip, then trim precisely
    clip_words, total = clip_words_of(ranges, words)

    segs = plan_segments(ranges, clip, a.reframe, shots)
    actcrop = f"crop={act['w']}:{act['h']}:{act['x']}:{act['y']}," if act["w"] != src["w"] or act["h"] != src["h"] else ""
    parts = []
    for k, (s, e) in enumerate(ranges):
        pieces = [p for p in segs if p[0] >= s - 1e-6 and p[1] <= e + 1e-6]
        parts.append(range_chain(k, s, e, pieces, seek, actcrop, act, W, H, a.fps))
    if len(ranges) > 1:
        parts.append("".join(f"[v{k}]" for k in range(len(ranges))) + f"concat=n={len(ranges)}:v=1:a=0[vc]")
        last = "[vc]"
    else:
        last = "[v0]"

    # (legacy, off by default) music-synced zoom on the composed frame, before any text
    fx_level = clip.get("zoom_fx", a.zoom_fx)
    fx = {"level": fx_level, "events": 0}
    if fx_level != "off" and src["has_audio"]:
        info = beats.analyse(os.path.abspath(a.video), ranges)
        cuts, off = [], 0.0
        for k, (s, e) in enumerate(ranges):
            cuts += [p[0] - s + off for p in segs if s < p[0] < e and p[0] >= s]  # shot cuts in clip time
            off += e - s
            if k:
                cuts.append(off - (e - s))  # range joins are cuts too
        events = beats.plan_zoom(info, sorted(cuts), total, fx_level)
        zf = beats.zoom_filter(events, W, H, a.fps)
        if zf:
            parts.append(f"{last}{zf}[vz]")
            last = "[vz]"
        fx = {"level": fx_level, "bpm": info["bpm"], "music": info["music"], "events": len(events),
              "punches": sum(e["type"] == "punch" for e in events)}

    tag = slugify(clip.get("market") or "", 8) if clip.get("market") else ""
    slug = f"clip_{idx:02d}{'_' + tag if tag else ''}_{slugify(clip.get('title', ''))}"
    cs = clip.get("caption_style") or {}
    chunks = clip.get("captions_override")
    show_caps = cs.get("enabled", not a.no_captions) and (clip_words or chunks)
    if show_caps:
        ass_name = f"{slug}.ass"
        with open(os.path.join(a.out_dir, ass_name), "w", encoding="utf-8") as f:
            preset = cs.get("preset", a.caption_preset)
            hl = cs.get("highlight") or (a.highlight if a.highlight_set else None)   # None: the preset's colour
            up = cs.get("uppercase", a.uppercase if a.uppercase_set else None)
            f.write(build_ass(clip_words, W, H, a.words_per_line, hl.lstrip("#") if hl else None, up,
                              size=float(cs.get("size", 1.0)), y=float(cs.get("y", 0.75)), chunks=chunks,
                              preset=preset, emphasis=cs.get("emphasis", a.emphasis)))
        fontsdir = ""
        fd = fonts_dir()
        if fd and os.path.isdir(fd):
            # copy the fonts next to the .ass so the filter path needs no escaping (Windows "C:" etc.)
            local = os.path.join(a.out_dir, "_fonts")
            os.makedirs(local, exist_ok=True)
            for fn in os.listdir(fd):
                if fn.lower().endswith((".otf", ".ttf")) and not os.path.exists(os.path.join(local, fn)):
                    shutil.copy(os.path.join(fd, fn), local)
            fontsdir = ":fontsdir=_fonts"
        parts.append(f"{last}ass={ass_name}{fontsdir}[vcap]")
        last = "[vcap]"

    extra_inputs = []
    hook = clip.get("hook_title")
    hs_style = clip.get("hook_style") or {}
    if hook and not a.no_hook and hs_style.get("enabled", True):
        secs = hs_style.get("seconds", clip.get("hook_seconds", 4.0))
        hs = total if secs in (None, "all", 0) else min(float(secs), total)
        png = f"{slug}_hook.png"
        if make_hook_png(hook, W, os.path.join(a.out_dir, png), uppercase=hs_style.get("uppercase", True),
                         size=float(hs_style.get("size", 1.0)), theme=hs_style.get("theme", "light"),
                         accent=a.highlight, font=hs_style.get("font", "Inter")):
            from PIL import Image
            bh = Image.open(os.path.join(a.out_dir, png)).height
            y = hook_y(hs_style, bh, H)
            extra_inputs = ["-loop", "1", "-t", f"{hs:.2f}", "-i", png]
            fade_out = f",fade=out:st={max(0, hs - 0.3):.2f}:d=0.3:alpha=1" if hs < total - 0.05 else ""
            parts.append(f"[1:v]format=rgba,fade=in:st=0:d=0.25:alpha=1{fade_out}[ttl]")
            parts.append(f"{last}[ttl]overlay=(W-w)/2:{y}:eof_action=pass[vh]")
            last = "[vh]"

    if clip.get("progress_bar", a.progress_bar) and total > 3:
        # thin bar filling left to right: viewers see how much is left and stay to the end
        bar_h = max(6, H // 160)
        col = (cs.get("highlight") or (a.highlight if a.highlight_set else
                                       caption_preset(cs.get("preset", a.caption_preset))["hl"])).lstrip("#")
        parts.append(f"color=c=0x{col}:s={W}x{bar_h}:r={a.fps}:d={total:.3f},format=rgba[pbar]")
        parts.append(f"{last}[pbar]overlay=x='-W+W*t/{total:.3f}':y=H-{bar_h}:eof_action=pass[vpb]")
        last = "[vpb]"

    if not a.no_fade and total > 2:
        parts.append(f"{last}fade=out:st={total - 0.3:.3f}:d=0.3[vf]")
        last = "[vf]"

    amap = []
    if src["has_audio"]:
        for k, (s, e) in enumerate(ranges):
            edge = (f",afade=t=in:d=0.012,afade=t=out:st={max(0, e - s - 0.012):.3f}:d=0.012"
                    if len(ranges) > 1 else "")
            parts.append(f"[0:a]atrim=start={s - seek:.3f}:end={e - seek:.3f},asetpts=PTS-STARTPTS{edge}[ar{k}]")
        chain = "".join(f"[ar{k}]" for k in range(len(ranges))) + f"concat=n={len(ranges)}:v=0:a=1"
        aopts = clip.get("audio") or {}
        m = None
        if not a.no_loudnorm and aopts.get("normalize", True):
            m = measure_loudness(os.path.abspath(a.video), ranges)
        parts.append(f"{chain},{audio_filter(m, aopts, total, not a.no_fade)}[aout]")
        amap = ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]

    out_name = f"{slug}.mp4"
    lays = "".join(("Z" if ex.get("zoom", 1) > 1.01 else "F") if l == "fill" else "B" for _, _, l, _, ex in segs)
    fxs = (f" fx={fx['level']}: {fx.get('bpm', 0):.0f} BPM{' music' if fx.get('music') else ''}, "
           f"{fx.get('punches', 0)} punch / {fx['events']} zoom events") if fx["level"] != "off" else ""
    jc = f" jumpcuts={len(ranges) - len(base_ranges)}" if jumpcut else ""
    print(f"[{slug}] {total:.1f}s{jc} ranges={base_ranges} shots={lays} (F=fill, Z=fill zoomed on face, B=fit/blur){fxs}", file=sys.stderr)
    enc = pick_encoder(a.encoder)

    def run(enc_name):
        venc, pix = ENCODERS[enc_name]
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{seek:.3f}", "-i", os.path.abspath(a.video), *extra_inputs,
               "-filter_complex", ";".join(parts), "-map", last, *amap, *venc, "-pix_fmt", pix,
               "-g", str(int(a.fps * 2)), "-movflags", "+faststart", "-t", f"{total:.3f}", out_name]
        return subprocess.run(cmd, cwd=a.out_dir, capture_output=True, text=True, encoding="utf-8", errors="replace")
    r = run(enc)
    if r.returncode != 0 and enc != "x264":  # GPU encoder refused this job: fall back to CPU
        print(f"   {enc} failed, using CPU encoder", file=sys.stderr)
        r = run("x264")
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + "\n".join(r.stderr.strip().splitlines()[-4:]))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{min(1.0, total / 2):.2f}", "-i", out_name,
                    "-frames:v", "1", "-vf", "scale=360:-2", f"{slug}.jpg"], cwd=a.out_dir)
    return {"file": out_name, "thumb": f"{slug}.jpg", "ranges": base_ranges, "pieces": ranges,
            "jump_cuts": len(ranges) - len(base_ranges), "length": round(total, 1),
            "words": len(clip_words), "shot_layouts": lays, "zoom_fx": fx}


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
    ap.add_argument("--encoder", default="auto", choices=["auto", "x264", "nvenc", "qsv", "amf"],
                    help="H.264 encoder: auto = GPU (NVIDIA/Intel/AMD) when available, else CPU x264")
    ap.add_argument("--zoom-fx", default="off", choices=["off", "light", "medium", "strong"],
                    help="(legacy) music-synced zoom pulses; off by default")
    ap.add_argument("--caption-preset", default="classic", choices=list(CAPTION_PRESETS),
                    help="caption typography: " + ", ".join(CAPTION_PRESETS))
    ap.add_argument("--emphasis", action="store_true", help="colour numbers, money and power words in captions")
    ap.add_argument("--jumpcut", action="store_true", help="cut pauses and filler sounds between phrases")
    ap.add_argument("--progress-bar", action="store_true", help="thin progress bar along the bottom")
    ap.add_argument("--jobs", type=int, default=0, help="clips rendered in parallel (default: 2 if >= 6 CPU cores)")
    a = ap.parse_args(argv)
    raw = argv if argv is not None else sys.argv[1:]
    a.highlight_set, a.uppercase_set = "--highlight" in raw, "--uppercase" in raw
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
    enc = pick_encoder(a.encoder)
    print(f"Encoder: {enc}", file=sys.stderr)
    jobs = a.jobs or (2 if (os.cpu_count() or 2) >= 6 else 1)
    todo = [(i, c) for i, c in enumerate(clips, 1) if not (only and i not in only and i in prev)]
    done = {}
    if jobs > 1 and len(todo) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = {i: ex.submit(render_clip, i, c, a, src, words, duration, shots, act) for i, c in todo}
            for i, f in futs.items():
                done[i] = f.result()
    else:
        for i, c in todo:
            done[i] = render_clip(i, c, a, src, words, duration, shots, act)
    results = [done.get(i) or prev[i] for i in range(1, len(clips) + 1)]
    write_report(os.path.join(a.out_dir, "report.md"), clips, results, a.video)
    json.dump({"clips": clips, "rendered": results}, open(prev_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"Done: {len(results)} clips + report.md in {a.out_dir} ({a.fps:g} fps)")


if __name__ == "__main__":
    main()
