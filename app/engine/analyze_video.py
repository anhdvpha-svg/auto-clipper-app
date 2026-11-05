#!/usr/bin/env python3
"""Look at the video itself: shots, loudness, motion, where the subject is, and the most intense stretches.

This is what makes clipping work for footage where the words aren't the point (gameplay, trailers,
sports, music, vlogs, b-roll) and what drives the per-shot "fill vs fit" framing OpusClip uses.

Usage:
  python analyze_video.py VIDEO --out-dir clipper_work [--start 0 --end 600] [--scene-threshold 0.3]

Writes to --out-dir:
  shots.json        [{id, start, end, dur, loud_db, motion, layout, crop_x, faces, reason}]
                    layout = "fill" (crop 9:16 around crop_x) or "fit" (whole frame over blurred bg)
  shots.txt         one readable line per shot
  highlights.txt    top windows by audio-visual intensity (candidates when there's little speech)
  sheets/sheet_NN.png  contact sheets: one frame per shot, labelled "#id mm:ss", with the planned
                    9:16 crop drawn on it (yellow) or "FIT". Open these to see the video.
"""
import argparse, json, os, re, subprocess, sys
import numpy as np

try:
    import cv2
    _CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if _CASCADE.empty():
        _CASCADE = None
except Exception:  # OpenCV is optional; saliency + motion still work without it
    cv2, _CASCADE = None, None

AW = 320  # analysis width


def probe(path):
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout)
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    w, h = int(v["width"]), int(v["height"])
    if str((v.get("tags") or {}).get("rotate", "0")) in ("90", "270", "-90"):
        w, h = h, w
    return {"w": w, "h": h, "duration": float(j["format"]["duration"]),
            "has_audio": any(s["codec_type"] == "audio" for s in j["streams"])}


def detect_active_area(path, start, end, w, h):
    """Find letterbox/pillarbox bars (black borders baked into the video) with ffmpeg cropdetect.
    Returns the picture area {x, y, w, h} in source pixels; the renderer crops to it first."""
    dur = max(1.0, end - start)
    votes = {}
    for k in range(6):  # sample a few points; bars are usually constant
        t = start + dur * (k + 0.5) / 6
        r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", f"{t:.2f}", "-i", path, "-t", "1.5", "-an",
                            "-vf", "cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)
        if m:
            votes[m[-1]] = votes.get(m[-1], 0) + 1
    if not votes:
        return {"x": 0, "y": 0, "w": w, "h": h}
    cw, ch, cx, cy = (int(v) for v in max(votes, key=votes.get))
    # ignore tiny trims (noise) and absurd ones (a dark scene, not bars)
    if (cw >= w - 8 and ch >= h - 8) or cw < w * 0.4 or ch < h * 0.4:
        return {"x": 0, "y": 0, "w": w, "h": h}
    return {"x": cx, "y": cy, "w": cw, "h": ch}


def detect_cuts(path, start, end, thr):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(start), "-to", str(end), "-i", path, "-an",
                        "-vf", f"scale={AW}:-2,select='gt(scene,{thr})',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return [start + float(t) for t in re.findall(r"pts_time:([0-9.]+)", r.stderr)]


def audio_db(path, start, end, step=0.25):
    """Mono loudness curve in dBFS, one value per `step` seconds."""
    sr = 8000
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-to", str(end), "-i", path, "-vn",
                        "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    x = np.frombuffer(r.stdout, dtype=np.float32)
    n = int(sr * step)
    if len(x) < n:
        return np.array([])
    x = x[: len(x) // n * n].reshape(-1, n)
    return 20 * np.log10(np.sqrt((x ** 2).mean(axis=1)) + 1e-6)


ACTIVE = None  # set in main(): crop filter for black bars


def grab(path, t, w=AW):
    vf = (f"crop={ACTIVE['w']}:{ACTIVE['h']}:{ACTIVE['x']}:{ACTIVE['y']}," if ACTIVE else "") + f"scale={w}:-2"
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                        "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    if not r.stdout:
        return None
    h = len(r.stdout) // (w * 3)
    return np.frombuffer(r.stdout[: w * h * 3], dtype=np.uint8).reshape(h, w, 3)


def saliency(gray):
    """Spectral-residual saliency (Hou & Zhang 2007) - cheap 'where would the eye go' map."""
    small = np.asarray(gray, dtype=np.float32)
    f = np.fft.fft2(small)
    logamp = np.log(np.abs(f) + 1e-6)
    k = np.ones((3, 3), np.float32) / 9
    pad = np.pad(logamp, 1, mode="edge")
    avg = sum(pad[i:i + logamp.shape[0], j:j + logamp.shape[1]] * k[i, j] for i in range(3) for j in range(3))
    sal = np.abs(np.fft.ifft2(np.exp(logamp - avg + 1j * np.angle(f)))) ** 2
    return sal / (sal.max() + 1e-9)


def faces_in(rgb):
    if _CASCADE is None:
        return []
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    found = _CASCADE.detectMultiScale(g, scaleFactor=1.15, minNeighbors=6, minSize=(int(rgb.shape[1] * 0.05),) * 2)
    return [(x / rgb.shape[1], (x + w) / rgb.shape[1], w / rgb.shape[1]) for x, y, w, h in found]


_HOG = None


def people_in(rgb):
    """Full-body person detector (OpenCV HOG, bundled - no model download). Catches people whose
    face isn't visible: sunglasses, side/back views, wide shots."""
    global _HOG
    if cv2 is None:
        return []
    if _HOG is None:
        _HOG = cv2.HOGDescriptor()
        _HOG.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    scale = 480 / rgb.shape[1]
    img = cv2.resize(rgb, (480, int(rgb.shape[0] * scale)))
    rects, weights = _HOG.detectMultiScale(img, winStride=(8, 8), padding=(8, 8), scale=1.05)
    out = []
    for (x, y, w, h), wt in zip(rects, np.ravel(weights) if len(rects) else []):
        if wt > 0.6 and h > img.shape[0] * 0.35:  # confident and reasonably large
            out.append((x / img.shape[1], (x + w) / img.shape[1], w / img.shape[1]))
    return out


def plan_shot(frames, src_ar, target_ar=9 / 16):
    """Decide fill vs fit for one shot and where to crop.

    fill: the subject fits in a 9:16 window -> crop to it (punchier, fills the phone screen)
    fit : the action is spread across the frame (wide shot, two people far apart, text/slides)
          -> keep the whole frame, centred over a blurred copy, as OpusClip does
    """
    if src_ar <= target_ar + 0.01:
        return {"layout": "fill", "crop_x": 0.5, "faces": 0, "reason": "already vertical"}
    cw = target_ar / src_ar  # crop width as a fraction of source width (0.316 for 16:9)
    per_frame = [faces_in(fr) for fr in frames]
    faces = []
    # keep a face only if one shows up near the same x in at least 2 sampled frames:
    # real faces persist through a shot, texture false-positives (bricks, shadows) flicker
    need = 2 if len(frames) >= 2 else 1
    for i, ff in enumerate(per_frame):
        for f in ff:
            cx = (f[0] + f[1]) / 2
            hits = sum(any(abs((g[0] + g[1]) / 2 - cx) < 0.12 for g in other)
                       for j, other in enumerate(per_frame) if j != i) + 1
            if hits >= need:
                faces.append(f)
    if faces:
        # Haar cascades throw false positives in textures; keep faces comparable to the biggest one
        big = max(f[2] for f in faces)
        faces = [f for f in faces if f[2] >= big * 0.55]
    if faces:
        lo = min(f[0] for f in faces)
        hi = max(f[1] for f in faces)
        if hi - lo <= cw * 0.9 or big >= cw * 0.6:  # fits, or a close-up: centre on the face
            return {"layout": "fill", "crop_x": round((lo + hi) / 2, 3), "faces": len(faces),
                    "reason": "face(s) fit in the vertical crop"}
        return {"layout": "fit", "crop_x": 0.5, "faces": len(faces),
                "reason": "faces too far apart for one vertical crop"}

    people = [p for fr in frames for p in people_in(fr)]
    if people:
        big = max(p[2] for p in people)
        people = [p for p in people if p[2] >= big * 0.6]
        lo, hi = min(p[0] for p in people), max(p[1] for p in people)
        if hi - lo <= cw * 1.05:
            return {"layout": "fill", "crop_x": round((lo + hi) / 2, 3), "faces": 0,
                    "reason": "person fits in the vertical crop"}
        if len({round((p[0] + p[1]) / 2, 1) for p in people}) > 1:
            return {"layout": "fit", "crop_x": 0.5, "faces": 0, "reason": "several people spread out"}

    grays = [fr.mean(axis=2) for fr in frames]
    energy = np.zeros(grays[0].shape[1])
    for g in grays:
        small = g[::2, ::2]
        s = saliency(small)
        energy += np.repeat(s.sum(axis=0), 2)[: len(energy)]
    for a, b in zip(grays, grays[1:]):  # motion: where things change between frames
        energy += np.abs(a - b).sum(axis=0) / 255.0 * 0.5
    energy = np.convolve(energy, np.ones(5) / 5, mode="same")
    total = energy.sum() + 1e-9
    W = len(energy)
    win = max(1, int(W * cw))
    csum = np.concatenate([[0], np.cumsum(energy)])
    sums = csum[win:] - csum[:-win]
    best = int(np.argmax(sums))
    coverage = sums[best] / total
    uniform = win / W  # coverage a featureless frame would give
    x = (best + win / 2) / W
    # the window must hold clearly more than its share of the action to be worth cropping
    if coverage >= max(0.45, uniform * 1.1):
        return {"layout": "fill", "crop_x": round(float(x), 3), "faces": 0,
                "reason": f"subject concentrated ({coverage:.0%} of activity in crop)"}
    return {"layout": "fit", "crop_x": 0.5, "faces": 0,
            "reason": f"action spread across frame ({coverage:.0%} in best crop)"}


def fmt(t):
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:04.1f}"


def contact_sheets(path, shots, out_dir, src_ar, cols=4, rows=4):
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 15)
    except Exception:
        font = ImageFont.load_default()
    os.makedirs(os.path.join(out_dir, "sheets"), exist_ok=True)
    tw = 300
    th = int(tw / src_ar)
    per = cols * rows
    paths = []
    for p in range(0, len(shots), per):
        page = shots[p:p + per]
        sheet = Image.new("RGB", (cols * (tw + 6) + 6, rows * (th + 30) + 6), (20, 20, 20))
        d = ImageDraw.Draw(sheet)
        for i, s in enumerate(page):
            fr = grab(path, (s["start"] + s["end"]) / 2, tw)
            if fr is None:
                continue
            im = Image.fromarray(fr).resize((tw, th))
            x0, y0 = 6 + (i % cols) * (tw + 6), 6 + (i // cols) * (th + 30)
            sheet.paste(im, (x0, y0 + 24))
            if s["layout"] == "fill":
                cw = tw * (9 / 16) / src_ar
                cx = x0 + s["crop_x"] * tw
                d.rectangle([cx - cw / 2, y0 + 24, cx + cw / 2, y0 + 24 + th - 1], outline=(255, 214, 0), width=3)
            label = f"#{s['id']} {fmt(s['start'])} {s['dur']:.1f}s {s['layout'].upper()} {s['loud_db']:.0f}dB"
            d.text((x0 + 2, y0 + 3), label, fill=(255, 255, 255), font=font)
        fp = os.path.join(out_dir, "sheets", f"sheet_{p // per + 1:02d}.png")
        sheet.save(fp)
        paths.append(fp)
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float)
    ap.add_argument("--scene-threshold", type=float, default=0.3)
    ap.add_argument("--min-shot", type=float, default=0.5, help="merge shots shorter than this (s)")
    ap.add_argument("--max-shot", type=float, default=8.0, help="re-plan long shots every N s")
    ap.add_argument("--no-sheets", action="store_true")
    ap.add_argument("--no-plan", action="store_true",
                    help="fast pass: cuts, loudness and highlights only (no framing plan, no sheets)")
    a = ap.parse_args(argv)
    os.makedirs(a.out_dir, exist_ok=True)
    info = probe(a.video)
    end = min(a.end or info["duration"], info["duration"])
    global ACTIVE
    ACTIVE = detect_active_area(a.video, a.start, end, info["w"], info["h"])
    if (ACTIVE["w"], ACTIVE["h"]) != (info["w"], info["h"]):
        print(f"Black bars found: picture area is {ACTIVE['w']}x{ACTIVE['h']} at ({ACTIVE['x']},{ACTIVE['y']})",
              file=sys.stderr)
    else:
        ACTIVE = None
    act = ACTIVE or {"x": 0, "y": 0, "w": info["w"], "h": info["h"]}
    src_ar = act["w"] / act["h"]

    print("Detecting scene cuts...", file=sys.stderr)
    cuts = [c for c in detect_cuts(a.video, a.start, end, a.scene_threshold) if a.start < c < end]
    bounds = [a.start] + cuts + [end]
    spans = []
    for s, e in zip(bounds, bounds[1:]):
        if spans and e - s < a.min_shot:
            spans[-1][1] = e  # too short to be its own shot: merge into previous
        else:
            spans.append([s, e])
    if len(spans) > 1 and spans[0][1] - spans[0][0] < a.min_shot:
        spans[1][0] = spans[0][0]
        spans.pop(0)
    # long takes (talking head, pan) get re-planned every max_shot seconds so the crop can follow
    split = []
    for s, e in spans:
        n = max(1, int(np.ceil((e - s) / a.max_shot)))
        step = (e - s) / n
        split += [[s + i * step, s + (i + 1) * step] for i in range(n)]

    db = audio_db(a.video, a.start, end) if info["has_audio"] else np.array([])
    shots = []
    print(f"Planning {len(split)} shots...", file=sys.stderr)
    for i, (s, e) in enumerate(split):
        if a.no_plan:
            plan, motion = {"layout": "fill", "crop_x": 0.5, "faces": 0, "reason": "not planned"}, 0.0
        else:
            ts = [s + (e - s) * f for f in (0.2, 0.5, 0.8)]
            frames = [f for f in (grab(a.video, t) for t in ts) if f is not None]
            if not frames:
                continue
            plan = plan_shot(frames, src_ar)
            motion = float(np.mean([np.abs(x.astype(np.int16) - y.astype(np.int16)).mean()
                                    for x, y in zip(frames, frames[1:])])) if len(frames) > 1 else 0.0
        if len(db):
            seg = db[int((s - a.start) / 0.25): max(int((s - a.start) / 0.25) + 1, int((e - a.start) / 0.25))]
            loud = float(np.mean(seg)) if len(seg) else -60.0
        else:
            loud = -60.0
        shots.append({"id": i, "start": round(s, 3), "end": round(e, 3), "dur": round(e - s, 2),
                      "loud_db": round(loud, 1), "motion": round(motion, 1), **plan})

    with open(os.path.join(a.out_dir, "shots.json"), "w") as f:
        json.dump({"source": os.path.abspath(a.video), "width": info["w"], "height": info["h"],
                   "active_area": act, "shots": shots}, f, indent=1)
    with open(os.path.join(a.out_dir, "shots.txt"), "w") as f:
        for s in shots:
            f.write(f"#{s['id']:<4} {fmt(s['start'])}-{fmt(s['end'])} {s['dur']:5.1f}s {s['layout']:4} "
                    f"x={s['crop_x']:.2f} loud={s['loud_db']:6.1f}dB motion={s['motion']:5.1f}  {s['reason']}\n")

    # intensity = loudness + motion + cut rate, scored over sliding windows
    hl = []
    if shots:
        L = np.array([s["loud_db"] for s in shots]); M = np.array([s["motion"] for s in shots])
        z = lambda v: (v - v.mean()) / (v.std() + 1e-6)
        score = z(L) + (z(M) if M.std() > 0 else 0)
        for win in (15, 30, 60):
            for i, s in enumerate(shots):
                j = i
                while j + 1 < len(shots) and shots[j + 1]["end"] - s["start"] <= win:
                    j += 1
                span = shots[j]["end"] - s["start"]
                if span < win * 0.6:
                    continue
                cuts_per_10s = (j - i + 1) / span * 10
                hl.append((float(score[i:j + 1].mean() + 0.15 * cuts_per_10s), s["start"], shots[j]["end"], win))
        hl.sort(reverse=True)
        picked = []
        for h in hl:
            if all(h[2] <= p[1] or h[1] >= p[2] for p in picked):
                picked.append(h)
            if len(picked) >= 8:
                break
        with open(os.path.join(a.out_dir, "highlights.txt"), "w") as f:
            f.write("# most intense stretches (loudness + motion + cut rate). Candidates, not verdicts.\n")
            for sc, s, e, w in picked:
                f.write(f"{fmt(s)}-{fmt(e)} ({e - s:.0f}s) score={sc:+.2f}\n")

    sheets = [] if (a.no_sheets or a.no_plan) else contact_sheets(a.video, shots, a.out_dir, src_ar)
    fills = sum(s["layout"] == "fill" for s in shots)
    print(f"{len(shots)} shots ({fills} fill, {len(shots) - fills} fit) -> {a.out_dir}/shots.json, shots.txt, "
          f"highlights.txt, {len(sheets)} contact sheet(s)")
    if cv2 is None:
        print("Note: OpenCV not installed - face detection skipped (pip install 'opencv-python-headless<5').",
              file=sys.stderr)


if __name__ == "__main__":
    main()
