#!/usr/bin/env python3
"""Music-aware zoom effects (OpusClip-style punch-ins, beat pulses and slow push-ins).

Listening:  onset envelope (spectral flux) -> tempo (autocorrelation with a 120 BPM prior)
            -> beat positions (dynamic programming, Ellis 2007) -> strong hits (onset peaks).
Planning:   per shot - punch-in zoom on strong hits, a gentle pulse on beats when the music has a clear
            pulse, a slow push-in/out on calm long shots. Effects never cross a cut.
Rendering:  an ffmpeg zoompan expression applied to the composed 9:16 frame (before text overlays).

  python beats.py VIDEO --start 42.9 --end 73.3        # print tempo, beats, hits for a stretch
"""
import argparse, json, subprocess, sys

import numpy as np

SR, HOP, NFFT = 22050, 512, 2048
FPS_ENV = SR / HOP  # onset-envelope frames per second (~43)

LEVELS = {  # punch amplitude, beat-pulse amplitude, push-in amount, min gap between punches (s)
    "off": (0.0, 0.0, 0.0, 99),
    "light": (0.07, 0.0, 0.04, 4.0),
    "medium": (0.11, 0.025, 0.06, 2.6),   # ~OpusClip: a punch every ~3 s on action/music
    "strong": (0.16, 0.045, 0.08, 1.4),
}


def load_audio(video, ranges):
    """Mono float32 at 22.05 kHz for the clip's ranges, concatenated in clip order."""
    parts = []
    for s, e in ranges:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", video,
                            "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True,
                           stdin=subprocess.DEVNULL)
        a = np.frombuffer(r.stdout, dtype=np.float32)
        want = int((e - s) * SR)
        parts.append(np.pad(a[:want], (0, max(0, want - len(a)))))
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def onset_envelope(y):
    if len(y) < NFFT:
        return np.zeros(1)
    win = np.hanning(NFFT).astype(np.float32)
    n = 1 + (len(y) - NFFT) // HOP
    idx = np.arange(NFFT)[None, :] + HOP * np.arange(n)[:, None]
    mag = np.abs(np.fft.rfft(y[idx] * win, axis=1))
    logm = np.log1p(100 * mag)
    flux = np.maximum(0, np.diff(logm, axis=0)).sum(axis=1)
    flux = np.concatenate([[0], flux])
    return flux


# A steady sound (a tone, hum, silence) has raw flux ~4 with little variation; music, speech and action
# soundtracks sit around 100-300 and vary a lot. Below these, nothing is an "accent" worth a zoom.
MIN_FLUX_MEDIAN, MIN_FLUX_CV, HIT_OVER_MEDIAN = 20.0, 0.35, 2.5


def normalise(raw):
    env = raw - np.convolve(raw, np.ones(16) / 16, mode="same")  # remove slow loudness drifts
    env = np.maximum(env, 0)
    return env / (env.max() + 1e-9)


def estimate_tempo(env):
    """(bpm, confidence 0..1). Autocorrelation over 60-200 BPM, log-normal prior centred on 120 BPM."""
    if len(env) < FPS_ENV * 4:
        return 0.0, 0.0
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    ac /= ac[0] + 1e-9
    lags = np.arange(int(FPS_ENV * 60 / 200), int(FPS_ENV * 60 / 60) + 1)
    lags = lags[lags < len(ac)]
    if not len(lags):
        return 0.0, 0.0
    bpm = 60 * FPS_ENV / lags
    prior = np.exp(-0.5 * (np.log2(bpm / 120) / 0.9) ** 2)
    score = ac[lags] * prior
    k = int(np.argmax(score))
    return float(bpm[k]), float(max(0.0, ac[lags[k]]))


def track_beats(env, bpm, tightness=100):
    """Beat times (s) by dynamic programming: strong onsets, spaced close to the tempo period."""
    if bpm <= 0 or len(env) < 4:
        return []
    period = FPS_ENV * 60 / bpm
    score = env.copy()
    back = -np.ones(len(env), dtype=int)
    lo, hi = int(round(period / 2)), int(round(period * 2))
    for t in range(len(env)):
        a, b = max(0, t - hi), t - lo
        if b <= a:
            continue
        prev = np.arange(a, b)
        cand = score[prev] - tightness * (np.log((t - prev) / period)) ** 2
        j = int(np.argmax(cand))
        score[t] = env[t] + cand[j]
        back[t] = prev[j]
    # start from the best-scoring frame in the last period and walk back
    t = int(np.argmax(score[-int(period) - 1:])) + len(env) - int(period) - 1
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats = np.array(beats[::-1])
    keep = env[beats] > 0.05 * env.max() if len(beats) else beats  # drop beats in silence
    return [float(b / FPS_ENV) for b in beats[keep]]


def strong_hits(env, raw, min_gap=0.4):
    """Accents worth a punch-in: onset peaks well above the local level and clearly above the background."""
    if len(env) < 3:
        return []
    thr = max(env.mean() + 2.2 * env.std(), np.percentile(env, 97))
    floor = HIT_OVER_MEDIAN * np.median(raw)
    peaks = [i for i in range(1, len(env) - 1) if env[i] >= thr and raw[i] >= floor
             and env[i] >= env[i - 1] and env[i] >= env[i + 1]]
    out, last = [], -1e9
    for i in sorted(peaks, key=lambda i: -env[i]):  # strongest first, keep spacing
        if all(abs(i - j) >= min_gap * FPS_ENV for j in out):
            out.append(i)
    return sorted((float(i / FPS_ENV), float(env[i])) for i in out)


def analyse(video, ranges):
    y = load_audio(video, ranges)
    raw = onset_envelope(y)
    med = float(np.median(raw)) if len(raw) else 0.0
    cv = float(raw.std() / (raw.mean() + 1e-9)) if len(raw) else 0.0
    dynamic = med >= MIN_FLUX_MEDIAN and cv >= MIN_FLUX_CV
    if not dynamic:  # steady or silent audio: no accents, no beat - only calm push-ins will be used
        return {"bpm": 0.0, "confidence": 0.0, "music": False, "beats": [], "hits": [], "dynamic": False,
                "duration": len(y) / SR}
    env = normalise(raw)
    bpm, conf = estimate_tempo(env)
    beats = track_beats(env, bpm) if conf > 0.12 else []
    return {"bpm": round(bpm, 1), "confidence": round(conf, 2), "music": conf > 0.25 and len(beats) >= 4,
            "beats": [round(b, 3) for b in beats],
            "hits": [(round(t, 3), round(v, 2)) for t, v in strong_hits(env, raw)], "dynamic": True,
            "duration": len(y) / SR}


def plan_zoom(info, cuts, total, level="medium"):
    """Zoom events in clip time. cuts = shot boundaries inside the clip (clip time, sorted)."""
    punch, pulse, push, gap = LEVELS.get(level, LEVELS["medium"])
    if punch == 0 or total <= 0:
        return []
    bounds = [0.0] + [c for c in cuts if 0.05 < c < total - 0.05] + [total]
    shots = list(zip(bounds, bounds[1:]))
    events, last_punch = [], -99.0
    for k, (s, e) in enumerate(shots):
        hits = [(t, v) for t, v in info["hits"] if s + 0.05 <= t < e - 0.25]
        used = []
        for t, v in sorted(hits, key=lambda x: -x[1]):
            if t - last_punch >= gap and all(abs(t - u) >= gap for u in used):
                used.append(t)
        for t in sorted(used):
            amp = punch * (0.75 + 0.25 * min(1.0, next(v for tt, v in hits if tt == t)))
            dur = min(0.7, e - t)  # snap in, ease back out before the next cut
            events.append({"type": "punch", "t": round(t, 3), "end": round(t + dur, 3), "amp": round(amp, 3)})
            last_punch = t
        if info.get("music") and pulse > 0:
            step = (2 if level == "strong" else 4) * (2 if info["bpm"] > 135 else 1)  # once per bar, not every beat
            for b in info["beats"][::step]:
                if s <= b < e - 0.2 and all(abs(b - u) > 0.35 for u in used):
                    events.append({"type": "pulse", "t": round(b, 3), "end": round(min(e, b + 0.3), 3),
                                   "amp": pulse})
        if not used and e - s >= 2.0 and push > 0:  # calm, long shot: Ken Burns, alternating direction
            events.append({"type": "push_in" if k % 2 == 0 else "push_out", "t": round(s, 3), "end": round(e, 3),
                           "amp": push})
    return events


def zoom_expr(events):
    """ffmpeg expression of the zoom factor over input time `it` (>= 1.0)."""
    terms = []
    for ev in events:
        a, b, amp = ev["t"], ev["end"], ev["amp"]
        if ev["type"] in ("punch", "pulse"):
            tau = 0.18 if ev["type"] == "punch" else 0.08
            attack = 0.05 if ev["type"] == "punch" else 0.03
            terms.append(f"if(between(it\\,{a}\\,{b})\\,{amp}*min(1\\,(it-{a})/{attack})*exp(-(it-{a})/{tau})\\,0)")
        else:
            d = max(0.01, b - a)
            x = f"((it-{a})/{d:.3f})" if ev["type"] == "push_in" else f"(({b}-it)/{d:.3f})"
            terms.append(f"if(between(it\\,{a}\\,{b})\\,{amp}*{x}*{x}*(3-2*{x})\\,0)")  # smoothstep ease
    return "1+" + "+".join(terms) if terms else "1"


def zoom_filter(events, W, H, fps):
    """Filter text applied to a W x H stream: zoom around the centre, one output frame per input frame."""
    if not events:
        return None
    z = zoom_expr(events)
    return (f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--start", type=float, default=0)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--level", default="medium", choices=list(LEVELS))
    a = ap.parse_args(argv)
    info = analyse(a.video, [(a.start, a.end)])
    print(json.dumps({k: v for k, v in info.items() if k not in ("beats",)}, indent=1))
    print("beats:", info["beats"][:24], "..." if len(info["beats"]) > 24 else "")
    for ev in plan_zoom(info, [], a.end - a.start, a.level):
        print(ev)


if __name__ == "__main__":
    main()
