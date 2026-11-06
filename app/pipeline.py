"""The whole job: link/file -> clips. Runs in a worker thread; talks to the UI through callbacks."""
import contextlib, datetime, glob, io, json, os, re, shutil, sys, threading

import numpy as np

from . import config, llm, offline, tools
from .downloader import Downloader, DownloadError
from .engine import analyze_video, fetch_video, render_clips, transcribe


def safe_name(text, maxlen=60):
    """Readable folder name that Windows accepts (keeps Vietnamese letters)."""
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", text or "")
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:maxlen].rstrip(" .") or "video"


def project_folder(day_dir, now, title):
    base = os.path.join(day_dir, f"{now.strftime('%Hh%M')} - {safe_name(title)}")
    path, n = base, 2
    while os.path.exists(path):
        path, n = f"{base} ({n})", n + 1
    return path


def hide(path):
    """Mark the work folder hidden on Windows (source video, transcript, plans) - it stays for re-editing."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)
        except Exception:
            pass


class JobError(Exception):
    """Shown to the user as-is."""


class Cancelled(Exception):
    pass


class _LineWriter(io.TextIOBase):
    """Forward engine prints (stdout/stderr) to the log callback, line by line."""

    def __init__(self, log):
        self.log, self.buf = log, ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.log("   " + line.rstrip())
        return len(s)


def _run(module, argv, log):
    """Call an engine script's main(); turn its sys.exit(message) into JobError."""
    w = _LineWriter(log)
    try:
        with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
            module.main([str(a) for a in argv])
    except SystemExit as e:
        if e.code not in (None, 0):
            raise JobError(str(e.code))


def is_url(s):
    return bool(re.match(r"^https?://", s.strip(), re.I))


LENGTHS = {"15-30": (15, 30), "30-60": (30, 60), "60-90": (60, 90)}


class Job:
    def __init__(self, source, cfg, log, progress, done):
        self.source = source.strip().strip('"')
        self.cfg = cfg
        self.log = log
        self.progress = progress      # progress(fraction 0..1, status text)
        self.done = done              # done(result dict or None, error text or None)
        self.cancel_flag = threading.Event()

    def cancel(self):
        self.cancel_flag.set()
        if getattr(self, "dl", None):
            self.dl.kill()

    def _check(self):
        if self.cancel_flag.is_set():
            raise Cancelled()

    def start(self):
        threading.Thread(target=self._thread, daemon=True).start()

    def _thread(self):
        try:
            self.done(self.run(), None)
            return
        except BaseException as e:
            tmp = getattr(self, "_tmp", None)
            if tmp and os.path.isdir(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
            out = getattr(self, "_out", None)  # half-made project with no clips: don't leave clutter
            if out and os.path.isdir(out) and not os.path.exists(os.path.join(out, "clips_rendered.json")):
                shutil.rmtree(out, ignore_errors=True)
            err = e
        try:
            raise err
        except Cancelled:
            self.done(None, "Đã huỷ.")
        except (JobError, llm.AIError) as e:
            self.done(None, str(e))
        except Exception as e:  # unexpected: show it, keep the app alive
            import traceback
            self.log(traceback.format_exc())
            self.done(None, f"Lỗi không mong muốn: {e}")

    # ------------------------------------------------------------------
    def run(self):
        cfg, log = self.cfg, self.log
        self.progress(0.02, "Chuẩn bị công cụ...")
        self.tools = tools.ensure(log)
        self._check()

        now = datetime.datetime.now()
        root = cfg.get("output_dir") or config.default_output_dir()
        day_dir = os.path.join(root, now.strftime("%Y-%m-%d"))       # one folder per day
        os.makedirs(day_dir, exist_ok=True)
        work = os.path.join(day_dir, f".tmp-{now.strftime('%H%M%S')}")  # renamed into the project below
        os.makedirs(work, exist_ok=True)
        self._tmp = work

        # 1. source video + subtitles + info
        info = {}
        if is_url(self.source):
            self.progress(0.05, "Đang tải video...")
            log(f"Tải video: {self.source}")
            self.dl = Downloader(self.tools["yt-dlp"], self.tools["deno"], os.path.dirname(self.tools["ffmpeg"]),
                                 log, lambda f, t: self.progress(0.05 + 0.10 * f, t), self.cancel_flag.is_set)
            try:
                video, subs, info = self.dl.download(self.source, work, browser=cfg.get("browser") or None)
            except DownloadError as e:
                raise JobError(str(e))
            if subs:
                log(f"Có phụ đề {'của kênh' if info.get('subtitle_kind') == 'manual' else 'tự động của YouTube'}: "
                    f"{os.path.basename(subs)}")
        else:
            video = self.source
            if not os.path.isfile(video):
                raise JobError("Không tìm thấy file video. Dán link hoặc chọn lại file.")
            subs, info = self._sidecars(video)
        self._check()

        title = info.get("title") or os.path.splitext(os.path.basename(video))[0]
        # project folder: <Lưu vào>/<YYYY-MM-DD>/<HHhMM - title>/ with clips, report and a hidden _work folder
        out = project_folder(day_dir, now, title)
        os.makedirs(out, exist_ok=True)
        self._out = out
        new_work = os.path.join(out, "_work")
        os.replace(work, new_work)
        if os.path.dirname(os.path.abspath(video)) == os.path.abspath(work):
            video = os.path.join(new_work, os.path.basename(video))
        if subs and os.path.dirname(os.path.abspath(subs)) == os.path.abspath(work):
            subs = os.path.join(new_work, os.path.basename(subs))
        work = new_work
        self._tmp = None
        hide(work)
        probe = render_clips.ffprobe_json(video)
        duration = float(probe["format"].get("duration") or 0)
        has_audio = any(s["codec_type"] == "audio" for s in probe["streams"])
        log(f"Video: {title} ({duration / 60:.1f} phút)")

        # 2. transcript
        self.progress(0.15, "Đang chép lời...")
        lang = cfg.get("language") if cfg.get("language") in ("vi", "en") else None
        tjson = os.path.join(work, "transcript.json")
        if subs:
            log(f"Dùng phụ đề có sẵn: {os.path.basename(subs)}")
            argv = [video, "--out-dir", work, "--srt", subs]
            if lang or info.get("language"):
                argv += ["--language", lang or info["language"]]
            _run(transcribe, argv, log)
        elif has_audio:
            log(f"Chép lời bằng Whisper ({cfg.get('whisper_model', 'small')}). Lần đầu sẽ tải model, có thể mất vài phút.")
            argv = [video, "--out-dir", work, "--model", cfg.get("whisper_model", "small")]
            if lang:
                argv += ["--language", lang]
            try:
                _run(transcribe, argv, log)
            except Cancelled:
                raise
            except Exception as e:  # model download blocked, decoder error...: carry on with the pictures
                log(f"Không chép lời được ({e}); tiếp tục chỉ với hình ảnh.")
        transcript = json.load(open(tjson, encoding="utf-8")) if os.path.exists(tjson) else {"segments": []}
        transcript.setdefault("duration", duration)
        words = sum(len(s["text"].split()) for s in transcript["segments"])
        wpm = words / max(duration / 60, 0.1)
        mode = "talk" if wpm >= 40 else "visual"
        log(f"Loại video: {'nói chuyện' if mode == 'talk' else 'hình ảnh / ít lời'} ({wpm:.0f} từ/phút)")
        self._check()

        # 3. audio-visual overview (fast pass): cuts, loudness, highlights
        self.progress(0.3, "Đang phân tích hình ảnh và âm thanh...")
        overview = os.path.join(work, "overview")
        _run(analyze_video, [video, "--out-dir", overview, "--no-plan"], log)
        hl_txt = open(os.path.join(overview, "highlights.txt"), encoding="utf-8").read()
        all_shots = json.load(open(os.path.join(overview, "shots.json")))["shots"]
        db = None
        if has_audio:
            db = analyze_video.audio_db(video, 0, duration)
        self._check()

        # 4. choose moments
        lo, hi = LENGTHS.get(cfg.get("length"), (30, 60))
        want = int(cfg.get("num_clips") or 0)
        self.count_note = ""
        if want:
            # N clips must fit in the video without overlapping: shorten the clips, or lower N, and say so
            usable = duration * 0.85  # slack: clips are cut on sentence/shot boundaries
            if want * lo > usable:
                new_lo = max(8, int(usable / want))
                if want * new_lo > usable:
                    want_fit = max(1, int(usable // 8))
                    self.count_note = (f"Video dài {duration / 60:.1f} phút chỉ đủ cho tối đa {want_fit} clip "
                                       f"không trùng nhau (ít nhất 8 giây mỗi clip), nên tạo {want_fit} clip.")
                    want, new_lo = want_fit, 8
                else:
                    self.count_note = (f"Video dài {duration / 60:.1f} phút không đủ cho {want} clip dài {lo}–{hi} "
                                       f"giây, nên mỗi clip được rút còn khoảng {new_lo}–{max(new_lo + 5, int(usable / want))} giây.")
                lo, hi = new_lo, max(new_lo + 5, min(hi, int(usable / want)))
                log(self.count_note)
        opts = {"num_clips": want, "min_len": lo, "max_len": hi,
                "topic": cfg.get("topic", "").strip(), "language": cfg.get("language"), "video_title": title}
        provider = cfg.get("provider", "offline")
        self.progress(0.45, "Đang chọn đoạn hay...")
        hl = self._parse_highlights(hl_txt)

        def offline_pick(o, exclude=()):
            if mode == "talk":
                return offline.select_talk(transcript, o, db, exclude=exclude)
            return offline.select_visual(hl, all_shots, o, duration, exclude=exclude)

        notes = ""
        if provider == "offline":
            log("Chọn đoạn ở chế độ Offline (không dùng AI).")
            clips = offline_pick(opts)
            if not clips and opts["topic"]:
                raise JobError(f"Không tìm thấy đoạn nào khớp chủ đề \"{opts['topic']}\".")
        else:
            ctx = {"title": title, "channel": info.get("channel"), "duration": duration,
                   "language": transcript.get("language") or info.get("language"), "mode": mode,
                   "chapters": info.get("chapters") or [], "description": info.get("description") or "",
                   "options": opts, "transcript_txt": self._transcript_txt(work), "highlights": hl_txt}
            if mode == "visual":
                ctx.update(self._visual_context(video, work, hl_txt, log))
            model, key = cfg["models"].get(provider), config.get_key(cfg, provider)
            clips, notes = llm.select_clips(provider, model, key, ctx, log)
            clips = self._clean(clips, duration, lo, hi, want)
            if want and len(clips) < want:
                # the model returned fewer than asked: ask once more for the missing ones, avoiding chosen ranges
                log(f"AI mới chọn {len(clips)}/{want} clip, đang hỏi thêm...")
                ctx2 = dict(ctx, options=dict(opts, num_clips=want - len(clips)),
                            exclude=[(c["start"], c["end"]) for c in clips])
                try:
                    more, _ = llm.select_clips(provider, model, key, ctx2, log)
                    clips = self._clean(clips + more, duration, lo, hi, want)
                except llm.AIError as e:
                    log(f"Không hỏi thêm được: {e}")
            if want and len(clips) < want and not opts["topic"]:
                extra = offline_pick(dict(opts, num_clips=want - len(clips)),
                                     exclude=[(c["start"], c["end"]) for c in clips])
                for c in extra:
                    c["reason"] = "Bổ sung tự động (offline) vì AI không chọn đủ số clip. " + c.get("reason", "")
                clips = self._clean(clips + extra, duration, lo, hi, want)
        clips = self._clean(clips, duration, lo, hi, want)
        if not clips:
            raise JobError("Không tìm được đoạn phù hợp. " + (notes or "Thử nới độ dài clip hoặc bỏ chủ đề."))
        if want and len(clips) < want:
            why = (f"chỉ có {len(clips)} đoạn khớp chủ đề \"{opts['topic']}\"" if opts["topic"]
                   else "không còn đoạn nào đủ dài mà không trùng các clip đã chọn")
            self.count_note = (self.count_note + " " if self.count_note else "") + \
                f"Tạo được {len(clips)}/{want} clip: {why}."
            log(self.count_note)
        if notes:
            log("Ghi chú của AI: " + notes)
        for i, c in enumerate(clips, 1):
            log(f"  {i}. [{c['score']['total']}] {c['title']} ({c['end'] - c['start']:.0f}s)")
        self._check()

        # 5. framing plan for just the chosen stretches
        shots_path = None
        if cfg.get("framing", "auto") == "auto":
            self.progress(0.55, "Đang lên khung hình cho từng cảnh...")
            merged, active = [], None
            for i, c in enumerate(clips):
                d = os.path.join(work, f"plan_{i + 1:02d}")
                _run(analyze_video, [video, "--out-dir", d, "--start", max(0, c["start"] - 0.5),
                                     "--end", min(duration, c["end"] + 0.5), "--no-sheets"], log)
                sj = json.load(open(os.path.join(d, "shots.json")))
                merged += sj["shots"]
                active = active or sj.get("active_area")
                self._check()
            shots_path = os.path.join(work, "shots.json")
            json.dump({"shots": merged, "active_area": active}, open(shots_path, "w"))

        # 6. render
        self.progress(0.65, f"Đang dựng {len(clips)} clip...")
        spec = [self._to_spec(c) for c in clips]
        clips_path = os.path.join(work, "clips.json")
        json.dump({"clips": spec}, open(clips_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        argv = ["--video", video, "--clips", clips_path, "--out-dir", out,
                "--highlight", cfg.get("highlight", "FFE600"), "--encoder", cfg.get("encoder", "auto"),
                "--zoom-fx", cfg.get("zoom_fx", "medium")]
        if transcript["segments"]:
            argv += ["--transcript", tjson]
        if shots_path:
            argv += ["--shots", shots_path]
        else:
            argv += ["--reframe", "blur" if cfg.get("framing") == "blur" else "center"]
        if not cfg.get("captions", True):
            argv.append("--no-captions")
        if cfg.get("uppercase", True):
            argv.append("--uppercase")
        if not cfg.get("hook", True):
            argv.append("--no-hook")
        # remember how this project was rendered, so the editor can re-render single clips later
        json.dump({"out_dir": out, "title": title, "source": self.source, "created": now.isoformat(timespec="seconds"),
                   "video": os.path.abspath(video), "work": work, "base_args": [x for i, x in enumerate(argv) if x != "--clips" and not (i and argv[i - 1] == "--clips")],
                   "transcript": tjson if transcript["segments"] else None},
                  open(os.path.join(out, "project.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        n_clips, seen = len(spec), [0]

        def render_log(line):
            log(line)
            if line.strip().startswith("[clip_"):
                seen[0] += 1
                self.progress(0.65 + 0.33 * (seen[0] - 1) / n_clips, f"Đang dựng clip {seen[0]}/{n_clips}...")
        _run(render_clips, argv, render_log)
        shutil.rmtree(os.path.join(out, "_fonts"), ignore_errors=True)
        for f in glob.glob(os.path.join(out, "*_hook.png")) + glob.glob(os.path.join(out, "*.ass")):
            os.remove(f)

        rendered = json.load(open(os.path.join(out, "clips_rendered.json"), encoding="utf-8"))["rendered"]
        results = [{**c, "index": k, "file": os.path.join(out, r["file"]), "thumb": os.path.join(out, r["thumb"]),
                    "length": r["length"]} for k, (c, r) in enumerate(zip(spec, rendered))]
        self.progress(1.0, "Xong!")
        log(f"Xong! {len(results)} clip trong: {out}")
        return {"out_dir": out, "clips": results, "work_dir": work, "note": self.count_note}

    # ------------------------------------------------------------------
    @staticmethod
    def _sidecars(video):
        """Subtitles/info saved next to a downloaded file (e.g. by the one-click downloader)."""
        stem = os.path.splitext(video)[0]
        info = {}
        for p in (stem + ".info.json",):
            if os.path.exists(p):
                try:
                    j = json.load(open(p, encoding="utf-8"))
                    info = {"title": j.get("title"), "channel": j.get("channel") or j.get("uploader"),
                            "language": j.get("language"), "description": (j.get("description") or "")[:3000],
                            "chapters": [{"title": c.get("title"), "start": c.get("start_time")}
                                         for c in j.get("chapters") or []]}
                except Exception:
                    pass
        cands = glob.glob(glob.escape(stem) + ".*.vtt") + glob.glob(glob.escape(stem) + ".*.srt") + \
            [p for p in (stem + ".srt", stem + ".vtt") if os.path.exists(p)]
        lang = (info.get("language") or "").split("-")[0]

        def rank(p):
            code = p[len(stem):].strip(".").rsplit(".", 1)[0]
            if not code or code in ("srt", "vtt"):
                return 0
            if lang and code == lang:
                return 1          # creator subs in the spoken language
            if code.endswith("-orig"):
                return 2          # YouTube speech recognition
            return 9              # other language: probably a machine translation
        cands = sorted(set(cands), key=rank)
        subs = cands[0] if cands and rank(cands[0]) < 9 else None
        if subs is None and not lang and len(cands) == 1:
            subs = cands[0]  # language unknown and a single subtitle file next to the video: use it
        return subs, info

    @staticmethod
    def _transcript_txt(work):
        p = os.path.join(work, "transcript.txt")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    @staticmethod
    def _parse_highlights(txt):
        out = []
        for line in txt.splitlines():
            m = re.match(r"(\d+):([\d.]+)-(\d+):([\d.]+) \(\d+s\) score=([-+\d.]+)", line)
            if m:
                s = int(m.group(1)) * 60 + float(m.group(2))
                e = int(m.group(3)) * 60 + float(m.group(4))
                out.append((float(m.group(5)), s, e))
        return out

    def _visual_context(self, video, work, hl_txt, log):
        """For visual videos: plan + contact sheets of the top highlight stretches, for the AI to look at."""
        sheets, shots_txt = [], []
        for i, (sc, s, e) in enumerate(self._parse_highlights(hl_txt)[:4]):
            d = os.path.join(work, f"hl_{i + 1}")
            _run(analyze_video, [video, "--out-dir", d, "--start", s, "--end", e], log)
            sheets += sorted(glob.glob(os.path.join(d, "sheets", "*.png")))[:1]
            shots_txt.append(open(os.path.join(d, "shots.txt"), encoding="utf-8").read())
        return {"sheets": sheets[:4], "shots_txt": "\n".join(shots_txt)[:20000]}

    @staticmethod
    def _clean(clips, duration, lo, hi, n):
        """Validate clips: clamp lengths (instead of dropping), resolve overlaps by trimming, keep the best n."""
        good = []
        for c in clips or []:
            try:
                s, e = float(c["start"]), float(c["end"])
            except (KeyError, TypeError, ValueError):
                continue
            s, e = max(0.0, s), min(duration, e)
            if e - s > hi * 1.25:
                e = s + hi
            if e - s < max(5, lo * 0.5):
                continue
            c["start"], c["end"] = s, e
            sc = c.get("score") or {}
            c["score"] = {k: int(sc.get(k, 0) or 0) for k in ("total", "hook", "emotion", "value", "standalone")}
            good.append(c)
        good.sort(key=lambda c: -c["score"]["total"])
        chosen = []
        for c in good:
            for d in chosen:  # overlapping a better clip: keep the part that doesn't overlap, if long enough
                if c["start"] < d["end"] and c["end"] > d["start"]:
                    left, right = (c["start"], d["start"]), (d["end"], c["end"])
                    c["start"], c["end"] = max((left, right), key=lambda r: r[1] - r[0])
            if c["end"] - c["start"] >= max(5, lo * 0.5) and \
                    all(c["end"] <= d["start"] or c["start"] >= d["end"] for d in chosen):
                chosen.append(c)
        if n:
            chosen = chosen[:n]
        return sorted(chosen, key=lambda c: c["start"])

    def _to_spec(self, c):
        spec = {"title": c.get("title") or "Clip", "start": c["start"], "end": c["end"],
                "hook": c.get("opening_line", ""), "score": c["score"], "reason": c.get("reason", ""),
                "captions": {"TikTok": c.get("caption_tiktok", ""), "Instagram Reels": c.get("caption_reels", ""),
                             "YouTube Shorts": c.get("caption_shorts", "")},
                "hashtags": c.get("hashtags") or []}
        if self.cfg.get("hook", True) and c.get("hook_title"):
            spec["hook_title"] = c["hook_title"]
        return spec
