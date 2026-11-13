#!/usr/bin/env python3
"""Transcribe a video/audio file into word-timestamped JSON + a readable timeline.

Outputs (in --out-dir):
  transcript.json  {"language", "duration", "segments": [{"id","start","end","text","words":[{"w","s","e"}]}]}
  transcript.txt   one line per segment: "[#12 01:23.4-01:29.0] text"  <- read this to pick clips

Usage:
  python transcribe.py VIDEO --out-dir work/            # faster-whisper (auto language: vi/en/...)
  python transcribe.py VIDEO --out-dir work/ --srt subs.srt   # import existing .srt/.vtt instead
Options: --model small|medium|large-v3 (default small), --language vi|en (default auto)
"""
import argparse, html, json, os, re, subprocess, sys


def probe_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
        return float(out.strip())
    except Exception:
        return None


def fmt(t):
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:04.1f}"


def ts_to_sec(ts):
    ts = ts.strip().replace(",", ".")
    parts = [float(p) for p in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def split_words(text, start, end):
    """Approximate per-word timing for subtitles that only have cue-level timing.
    Time is distributed proportionally to word length, which is close enough for captions."""
    toks = text.split()
    if not toks:
        return []
    total = sum(len(t) + 1 for t in toks)
    words, cur = [], start
    for t in toks:
        d = (end - start) * (len(t) + 1) / total
        words.append({"w": t, "s": round(cur, 3), "e": round(cur + d, 3)})
        cur += d
    return words


# sound/music annotations in YouTube & broadcast subtitles: [Music], [Applause], (cười), ♪ ... ♪
NOISE = re.compile(r"\[[^\]]*\]|\([^)]*(?:music|nhạc|applause|vỗ tay|laugh|cười)[^)]*\)|[♪♫]+", re.I)


def clean(text):
    # tags first (<c>, <i>), then entities (&gt;&gt; is the ">>" speaker-change mark of YouTube captions,
    # &amp; &#39; ...), then the speaker marks and sound annotations themselves
    t = html.unescape(re.sub(r"<[^>]+>", "", text))
    t = re.sub(r"^\s*-\s+|>>+|&gt;", " ", t)
    return re.sub(r"\s+", " ", NOISE.sub(" ", t)).strip()


WORD_TAG = re.compile(r"<(\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})>")


def from_youtube_auto_vtt(raw):
    """YouTube auto-captions: rolling cues where the new line carries per-word timestamps,
    e.g. `word<00:00:01.520><c> next</c><00:00:01.900><c> words</c>`. Lines without tags are
    repeats of the previous line, so only tagged lines are read - this gives real word timings
    and no duplicated text."""
    words = []
    # Split on cue headers, not blank lines: YouTube puts whitespace-only lines inside cues.
    headers = list(re.finditer(r"^([\d:.,]+)\s*-->\s*([\d:.,]+).*$", raw, flags=re.M))
    for i, m in enumerate(headers):
        cue_s, cue_e = ts_to_sec(m.group(1)), ts_to_sec(m.group(2))
        body = raw[m.end(): headers[i + 1].start() if i + 1 < len(headers) else len(raw)]
        for line in body.splitlines():
            if not WORD_TAG.search(line):
                continue
            parts = WORD_TAG.split(line)  # [text, ts, text, ts, text, ...]
            times = [cue_s] + [ts_to_sec(t) for t in parts[1::2]]
            for t, chunk in zip(times, parts[0::2]):
                w = clean(chunk)
                if w:
                    words.append({"w": w, "s": round(t, 3), "e": round(cue_e, 3)})
    words.sort(key=lambda w: w["s"])
    for i in range(len(words) - 1):  # a word lasts until the next one starts (capped)
        words[i]["e"] = round(min(words[i]["e"], words[i + 1]["s"], words[i]["s"] + 1.5), 3)
    if words:
        words[-1]["e"] = round(min(words[-1]["e"], words[-1]["s"] + 1.5), 3)
    # auto-captions have little punctuation: start a new segment on a pause, sentence end, or length
    segs, cur = [], []
    for w in words:
        if cur and (w["s"] - cur[-1]["e"] > 0.7 or len(cur) >= 18 or re.search(r"[.!?]$", cur[-1]["w"])):
            segs.append(cur)
            cur = []
        cur.append(w)
    if cur:
        segs.append(cur)
    return [{"start": s[0]["s"], "end": s[-1]["e"], "text": " ".join(w["w"] for w in s), "words": s}
            for s in segs]


def from_subtitles(path):
    raw = open(path, encoding="utf-8-sig").read().replace("\r", "")
    if path.lower().endswith(".vtt") and WORD_TAG.search(raw):
        return None, from_youtube_auto_vtt(raw)
    blocks = re.split(r"\n\s*\n", raw)
    segs = []
    for b in blocks:
        m = re.search(r"([\d:.,]+)\s*-->\s*([\d:.,]+)", b)
        if not m:
            continue
        s, e = ts_to_sec(m.group(1)), ts_to_sec(m.group(2))
        lines = b[m.end():].strip().splitlines()
        text = clean(" ".join(lines))
        if not text:
            continue
        # VTT often repeats the previous line in rolling captions; skip exact repeats
        if segs and segs[-1]["text"] == text:
            segs[-1]["end"] = e
            continue
        segs.append({"start": s, "end": e, "text": text, "words": split_words(text, s, e)})
    return None, segs


def load_audio(path, sr=16000):
    """Mono float32 PCM at 16 kHz - the input Whisper expects."""
    import numpy as np
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0 or not r.stdout:
        sys.exit("Could not decode the audio: " + r.stderr.decode("utf-8", "replace").strip()[-300:])
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def from_whisper(path, model_name, language):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper is not installed. Run: pip install faster-whisper  "
                 "(or pass --srt with an existing subtitle file)")
    model = WhisperModel(model_name, device="auto", compute_type="int8")
    # Decode with ffmpeg ourselves: faster-whisper's own decoder (PyAV) breaks across PyAV releases
    # (e.g. "open() got an unexpected keyword argument 'metadata_errors'").
    it, info = model.transcribe(load_audio(path), language=language, word_timestamps=True,
                                vad_filter=True, beam_size=5)
    segs = []
    for s in it:
        words = [{"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)}
                 for w in (s.words or []) if w.word.strip()]
        segs.append({"start": round(s.start, 3), "end": round(s.end, 3),
                     "text": s.text.strip(), "words": words})
        print(f"  {fmt(s.start)} {s.text.strip()[:70]}", file=sys.stderr)
    return info.language, segs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--srt", help="existing .srt/.vtt to import instead of running Whisper")
    ap.add_argument("--model", default="small")
    ap.add_argument("--language", default=None, help="force language code, e.g. vi or en")
    a = ap.parse_args(argv)
    os.makedirs(a.out_dir, exist_ok=True)

    if a.srt:
        lang, segs = from_subtitles(a.srt)
        lang = a.language or lang
    else:
        lang, segs = from_whisper(a.media, a.model, a.language)

    for i, s in enumerate(segs):
        s["id"] = i
    data = {"source": os.path.abspath(a.media), "language": lang,
            "duration": probe_duration(a.media), "segments": segs}
    with open(os.path.join(a.out_dir, "transcript.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out_dir, "transcript.txt"), "w", encoding="utf-8") as f:
        f.write(f"# language={lang} duration={data['duration']}s segments={len(segs)}\n")
        for s in segs:
            f.write(f"[#{s['id']} {fmt(s['start'])}-{fmt(s['end'])}] {s['text']}\n")
    print(f"Wrote {len(segs)} segments to {a.out_dir}/transcript.{{json,txt}} (language={lang})")


if __name__ == "__main__":
    main()
