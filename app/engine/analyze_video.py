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
FW = 640  # face-detection width: small faces in wide shots need more pixels than the 320 px analysis frames


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


def grab_pair(path, t, w=FW):
    """Two frames 1/8 s apart (one ffmpeg call): the difference shows who talks and what moves."""
    vf = (f"crop={ACTIVE['w']}:{ACTIVE['h']}:{ACTIVE['x']}:{ACTIVE['y']}," if ACTIVE else "") + f"fps=8,scale={w}:-2"
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "2",
                        "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    if not r.stdout:
        return None
    n = len(r.stdout)
    for k in (2, 1):
        if n % k == 0 and (n // k) % (w * 3) == 0:
            h = n // k // (w * 3)
            arr = np.frombuffer(r.stdout, dtype=np.uint8).reshape(k, h, w, 3)
            return arr[0], (arr[1] if k == 2 else None), t
    return None


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


def _face_model():
    """YuNet (OpenCV Zoo, MIT) - far better than Haar on small, turned and dimly lit faces."""
    here = os.path.dirname(os.path.abspath(__file__))
    name = "face_detection_yunet_2023mar.onnx"
    cands = [os.environ.get("AUTOCLIPPER_FACE_MODEL") or "", os.path.join(here, name),
             os.path.join(here, "..", "..", "assets", "models", name), os.path.join(here, "..", "assets", name)]
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, "assets", "models", name))
    return next((c for c in cands if c and os.path.exists(c)), None)


_YUNET = []


def _yunet(w, h):
    if cv2 is None or not hasattr(cv2, "FaceDetectorYN"):
        return None
    if not _YUNET:
        path = _face_model()
        try:
            _YUNET.append(cv2.FaceDetectorYN.create(path, "", (w, h), 0.6, 0.3, 5000) if path else None)
        except Exception:
            _YUNET.append(None)
    det = _YUNET[0]
    if det is not None:
        det.setInputSize((w, h))
    return det


def faces_in(rgb):
    """Faces as dicts with box fractions (x0, x1, y0, y1), score and a mouth box (for 'who is talking')."""
    H, W = rgb.shape[:2]
    det = _yunet(W, H)
    out = []
    if det is not None:
        _, found = det.detect(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        for f in (found if found is not None else []):
            x, y, w, h = (float(v) for v in f[:4])
            if w < W * 0.022:
                continue
            mx0, mx1 = min(f[10], f[12]), max(f[10], f[12])
            my = (f[11] + f[13]) / 2
            out.append({"x0": max(0.0, x / W), "x1": min(1.0, (x + w) / W), "y0": max(0.0, y / H),
                        "y1": min(1.0, (y + h) / H), "score": float(f[14]),
                        "mouth": (mx0 - w * 0.1, my - h * 0.14, mx1 + w * 0.1, my + h * 0.16)})
        return out
    if _CASCADE is None:
        return []
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    for x, y, w, h in _CASCADE.detectMultiScale(g, scaleFactor=1.15, minNeighbors=6, minSize=(int(W * 0.05),) * 2):
        out.append({"x0": x / W, "x1": (x + w) / W, "y0": y / H, "y1": (y + h) / H, "score": 0.7,
                    "mouth": (x + w * 0.2, y + h * 0.62, x + w * 0.8, y + h * 0.95)})
    return out


def _region_change(a, b, box):
    H, W = a.shape[:2]
    x0, y0, x1, y1 = (int(max(0, min(lim, v))) for v, lim in zip(box, (W, H, W, H)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return 0.0
    return float(np.abs(a[y0:y1, x0:x1].astype(np.int16) - b[y0:y1, x0:x1].astype(np.int16)).mean())


def speaking(face, fr_a, fr_b):
    """Mouth movement between two frames 1/8 s apart, minus the movement of the whole head
    (camera moves / head turns move both): high for the person who is talking."""
    H, W = fr_a.shape[:2]
    mouth = _region_change(fr_a, fr_b, face["mouth"])
    head = _region_change(fr_a, fr_b, (face["x0"] * W, face["y0"] * H, face["x1"] * W, face["y1"] * H))
    return max(0.0, mouth - head * 0.8)


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


def face_tracks(pairs):
    """Detect faces in every sampled frame and link them across frames by position.
    Real faces persist through a shot; texture false-positives flicker and are dropped."""
    per = []
    for k, (fa, fb, t) in enumerate(pairs):
        ff = faces_in(fa)
        for f in ff:
            f["speak"] = speaking(f, fa, fb) if fb is not None else 0.0
            f["k"], f["t"] = k, t
        per.append(ff)
    tracks = []
    for ff in per:
        for f in ff:
            cx = (f["x0"] + f["x1"]) / 2
            w = f["x1"] - f["x0"]
            best = None
            for tr in tracks:
                last = tr[-1]
                if last["k"] == f["k"]:
                    continue
                d = abs((last["x0"] + last["x1"]) / 2 - cx)
                if d < max(0.07, w * 1.2) and (best is None or d < best[0]):
                    best = (d, tr)
            if best:
                best[1].append(f)
            else:
                tracks.append([f])
    need = 2 if len(pairs) >= 3 else 1
    keep = [tr for tr in tracks if len(tr) >= need or max(f["score"] for f in tr) >= 0.92]
    out = []
    for tr in keep:
        out.append({"cx": float(np.median([(f["x0"] + f["x1"]) / 2 for f in tr])),
                    "cy": float(np.median([(f["y0"] + f["y1"]) / 2 for f in tr])),
                    "w": float(np.median([f["x1"] - f["x0"] for f in tr])),
                    "h": float(np.median([f["y1"] - f["y0"] for f in tr])),
                    "x0": min(f["x0"] for f in tr), "x1": max(f["x1"] for f in tr),
                    "speak": float(np.mean([f["speak"] for f in tr])), "n": len(tr),
                    "path": [(f["t"], (f["x0"] + f["x1"]) / 2) for f in tr]})
    return out


def _zoom_for(face_h, group_w, cw):
    """Punch in on small faces (wide shots) so the expression reads on a phone: aim for a face about
    17% of the frame height, never more than 1.45x (upscaling), and keep the whole group in the crop."""
    if face_h <= 0 or face_h >= 0.13:
        return 1.0
    z = min(1.45, 0.17 / face_h, cw * 0.92 / max(group_w, 1e-3))
    return 1.0 if z < 1.12 else (1.4 if z >= 1.35 else 1.2)


def _track_path(tr, cw):
    pts = sorted(tr["path"])
    xs = [p[1] for p in pts]
    if len(pts) < 2 or max(xs) - min(xs) < cw * 0.12:
        return None
    return [[round(t, 3), round(x, 4)] for t, x in pts]


def action_window(pairs, cw):
    """Where the action is: the 9:16 window holding most of the movement (and, second, of what stands out).
    Returns (centre x, share of the activity inside the window, share a featureless frame would give)."""
    frames = [p[0] for p in pairs]
    small = [cv2.resize(f, (AW, int(f.shape[0] * AW / f.shape[1]))) if cv2 is not None and f.shape[1] != AW else f
             for f in frames]
    grays = [fr.mean(axis=2) for fr in small]
    energy = np.zeros(grays[0].shape[1])
    for g in grays:
        sal = saliency(g[::2, ::2])
        energy += np.repeat(sal.sum(axis=0), 2)[: len(energy)] * 0.6
    motion = np.zeros_like(energy)
    for (fa, fb, _), a in zip(pairs, small):
        if fb is not None:
            b = fb if fb.shape[1] == AW else cv2.resize(fb, (AW, int(fb.shape[0] * AW / fb.shape[1])))
            d = np.abs(a.mean(axis=2) - b.mean(axis=2))
            d[d < 6] = 0  # sensor noise / compression shimmer
            motion += d.sum(axis=0)[: len(motion)]
    if motion.sum() > 0:
        energy = energy / (energy.sum() + 1e-9) + motion / motion.sum() * 1.5
    energy = np.convolve(energy, np.ones(5) / 5, mode="same")
    total = energy.sum() + 1e-9
    W = len(energy)
    win = max(1, int(W * cw))
    csum = np.concatenate([[0], np.cumsum(energy)])
    sums = csum[win:] - csum[:-win]
    best = int(np.argmax(sums))
    return (best + win / 2) / W, float(sums[best] / total), win / W


def plan_shot(pairs, src_ar, target_ar=9 / 16):
    """Decide fill vs fit for one shot and where to point the 9:16 window.

    pairs: [(frame, frame 1/8 s later, source time)] sampled across the shot.
    fill: crop on a face (the one talking, or the group if it fits), else on a person, else on the action
    fit : the subject is spread across the frame -> whole frame over a blurred copy, as OpusClip does
    """
    if src_ar <= target_ar + 0.01:
        return {"layout": "fill", "crop_x": 0.5, "faces": 0, "reason": "already vertical"}
    cw = target_ar / src_ar  # crop width as a fraction of source width (0.316 for 16:9)
    frames = [p[0] for p in pairs]
    tracks = face_tracks(pairs)
    if tracks:
        big = max(t["w"] for t in tracks)
        tracks = [t for t in tracks if t["w"] >= big * 0.45]
    if tracks:
        lo, hi = min(t["x0"] for t in tracks), max(t["x1"] for t in tracks)
        if hi - lo <= cw * 0.92:  # everyone fits: centre on the group
            focus = max(tracks, key=lambda t: t["w"])
            z = _zoom_for(focus["h"], hi - lo, cw)
            return {"layout": "fill", "crop_x": round((lo + hi) / 2, 4), "crop_y": round(focus["cy"], 4), "zoom": z,
                    "track": _track_path(focus, cw) if len(tracks) == 1 else None, "faces": len(tracks),
                    "focus": "face", "reason": f"{len(tracks)} face(s) fit in the vertical crop" + (f", zoom {z}x" if z > 1 else "")}
        # faces too far apart for one crop: follow the one who is talking, or a clearly dominant face
        by_speak = sorted(tracks, key=lambda t: -t["speak"])
        by_size = sorted(tracks, key=lambda t: -t["w"])
        pick, why = None, ""
        if len(tracks) == 1:  # one face wider than the crop: a big close-up
            pick, why = tracks[0], "close-up"
        elif by_speak[0]["speak"] > 2.0 and by_speak[0]["speak"] > 1.6 * (by_speak[1]["speak"] + 0.3):
            pick, why = by_speak[0], "speaker"
        elif by_size[0]["w"] >= 1.5 * by_size[1]["w"]:
            pick, why = by_size[0], "main face"
        if pick:
            z = _zoom_for(pick["h"], pick["x1"] - pick["x0"], cw)
            return {"layout": "fill", "crop_x": round(pick["cx"], 4), "crop_y": round(pick["cy"], 4), "zoom": z,
                    "track": _track_path(pick, cw), "faces": len(tracks), "focus": why,
                    "reason": f"{len(tracks)} faces apart, following the {why}"}
        return {"layout": "fit", "crop_x": 0.5, "faces": len(tracks), "focus": "group",
                "reason": "faces too far apart for one vertical crop"}

    action = action_window(pairs, cw)
    per = [people_in(fr) for fr in frames[::2]]
    people = [p for ff in per for p in ff]
    if len(per) >= 2:  # a person must show up near the same place in two sampled frames
        people = [p for p in people if sum(any(abs((q[0] + q[1]) / 2 - (p[0] + p[1]) / 2) < 0.1 for q in ff)
                                            for ff in per) >= 2]
    if people:
        big = max(p[2] for p in people)
        people = [p for p in people if p[2] >= big * 0.6]
        lo, hi = min(p[0] for p in people), max(p[1] for p in people)
        far = abs((lo + hi) / 2 - action[0]) > cw * 0.6 and action[1] >= max(0.45, action[2] * 1.1)
        if hi - lo <= cw * 1.05 and not far:  # (a "person" away from all the movement is usually a coat rack)
            return {"layout": "fill", "crop_x": round((lo + hi) / 2, 3), "faces": 0, "focus": "person",
                    "reason": "person fits in the vertical crop"}
        if len({round((p[0] + p[1]) / 2, 1) for p in people}) > 1:
            return {"layout": "fit", "crop_x": 0.5, "faces": 0, "reason": "several people spread out"}

    # no one visible: point the window at the action (what moves) and, second, at what stands out
    x, coverage, uniform = action
    if coverage >= max(0.45, uniform * 1.1):
        return {"layout": "fill", "crop_x": round(float(x), 3), "faces": 0, "focus": "action",
                "reason": f"action concentrated ({coverage:.0%} of activity in crop)"}
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
                z = s.get("zoom", 1.0) or 1.0
                cw, chh = tw * (9 / 16) / src_ar / z, th / z
                cx = x0 + s["crop_x"] * tw
                cy = min(max(s.get("crop_y", 0.5) * th - chh * 0.4, 0), th - chh)
                d.rectangle([cx - cw / 2, y0 + 24 + cy, cx + cw / 2, y0 + 24 + cy + chh - 1], outline=(255, 214, 0), width=3)
            label = f"#{s['id']} {fmt(s['start'])} {s['dur']:.1f}s {s['layout'].upper()} {s['loud_db']:.0f}dB"
            d.text((x0 + 2, y0 + 3), label, fill=(255, 255, 255), font=font)
        fp = os.path.join(out_dir, "sheets", f"sheet_{p // per + 1:02d}.png")
        sheet.save(fp)
        paths.append(fp)
    return paths


def framing_sheet(path, shots, out_path, src_ar, cols=3):
    """Numbered frames with the planned crop and a 0-1 ruler under each, for an AI to pick the framing."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
        small = ImageFont.truetype("DejaVuSans.ttf", 13)
    except Exception:
        font = small = ImageFont.load_default()
    tw = 420
    th = int(tw / src_ar)
    rows = (len(shots) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + 10) + 10, rows * (th + 34) + 10), (16, 16, 16))
    d = ImageDraw.Draw(sheet)
    for i, s in enumerate(shots):
        fr = grab(path, (s["start"] + s["end"]) / 2, tw)
        x0, y0 = 10 + (i % cols) * (tw + 10), 10 + (i // cols) * (th + 34)
        if fr is not None:
            sheet.paste(Image.fromarray(fr).resize((tw, th)), (x0, y0))
        if s.get("layout") == "fill":
            z = s.get("zoom", 1.0) or 1.0
            cw, chh = tw * (9 / 16) / src_ar / z, th / z
            cx = x0 + s["crop_x"] * tw
            cy = min(max(s.get("crop_y", 0.5) * th - chh * 0.4, 0), th - chh)
            d.rectangle([cx - cw / 2, y0 + cy, cx + cw / 2, y0 + cy + chh - 1], outline=(255, 214, 0), width=3)
        d.rectangle([x0, y0, x0 + 58, y0 + 30], fill=(0, 0, 0))
        d.text((x0 + 6, y0 + 3), f"#{s['id']}", fill=(255, 255, 255), font=font)
        ry = y0 + th + 4  # ruler
        d.line([x0, ry + 6, x0 + tw, ry + 6], fill=(160, 160, 160), width=1)
        for k in range(11):
            x = x0 + tw * k / 10
            d.line([x, ry + (0 if k % 5 == 0 else 3), x, ry + 9], fill=(200, 200, 200), width=1)
            if k % 5 == 0:
                d.text((min(x - 8, x0 + tw - 26), ry + 11), f"{k / 10:.1f}", fill=(200, 200, 200), font=small)
    sheet.save(out_path)
    return out_path


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
            fr = (0.15, 0.5, 0.85) if e - s < 4 else (0.1, 0.3, 0.5, 0.7, 0.9)
            pairs = [p for p in (grab_pair(a.video, s + (e - s) * f) for f in fr) if p is not None]
            if not pairs:
                continue
            plan = plan_shot(pairs, src_ar)
            frames = [p[0][::2, ::2] for p in pairs]
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
                    f"x={s['crop_x']:.2f} z={s.get('zoom', 1):.1f} loud={s['loud_db']:6.1f}dB motion={s['motion']:5.1f}  {s['reason']}\n")

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
