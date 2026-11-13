"""Entry point for the Auto Clipper app (also what PyInstaller freezes).

  AutoClipper.exe                 open the app
  AutoClipper.exe --selftest DIR  headless end-to-end check used by the build (writes DIR/selftest.log)
"""
import os, subprocess, sys

# A windowed (no-console) build has no stdout/stderr; give prints somewhere to go.
if sys.stdout is None or sys.stderr is None:
    _null = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _null
    sys.stderr = sys.stderr or _null

# On Windows, stop every ffmpeg/yt-dlp call from flashing a console window.
if os.name == "nt":
    _Popen = subprocess.Popen

    class _NoWindowPopen(_Popen):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("creationflags", 0x08000000)  # CREATE_NO_WINDOW
            # a windowed app has no usable stdin; child tools (yt-dlp, deno, ffmpeg) can hang reading it
            kwargs.setdefault("stdin", subprocess.DEVNULL)
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoWindowPopen


# fonts shipped with the app (hook cards and captions); set before the engine renders anything
try:
    _base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    os.environ.setdefault("AUTOCLIPPER_FONTS", os.path.join(_base, "assets", "fonts"))
    os.environ.setdefault("AUTOCLIPPER_FACE_MODEL",
                          os.path.join(_base, "assets", "models", "face_detection_yunet_2023mar.onnx"))
except Exception:
    pass


def selftest(out_dir):
    """Download the tools, build a synthetic talking video, run the whole offline pipeline, open the window."""
    import json, threading, time
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "selftest.log")
    logf = open(log_path, "w", encoding="utf-8")

    def log(m):
        logf.write(str(m) + "\n")
        logf.flush()

    try:
        from app import config, tools, pipeline
        import cv2, numpy, PIL, anthropic, httpx, faster_whisper  # noqa: F401  (bundling check)
        log(f"imports ok; cv2 {cv2.__version__}; cascade "
            f"{os.path.exists(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')}")
        from app.engine import analyze_video
        assert analyze_video._yunet(640, 360) is not None, f"YuNet face model not loaded ({analyze_video._face_model()})"
        log("yunet ok")
        ff = tools.ensure(log)["ffmpeg"]
        video = os.path.join(out_dir, "test.mp4")
        subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=70",
                        "-f", "lavfi", "-i", "sine=frequency=330:duration=70", "-shortest",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", video], check=True)
        lines = ["Xin chào các bạn, hôm nay mình kể một bí mật.",
                 "Năm 2018 mình mất tám trăm triệu chỉ trong ba tuần.",
                 "Đó là bài học đắt giá nhất cuộc đời mình.",
                 "Tại sao lại như vậy? Vì mình không hiểu thứ mình đầu tư.",
                 "Đừng bao giờ bỏ tiền vào thứ bạn không giải thích được.",
                 "Quy tắc số một của đầu tư là hiểu rõ công ty.",
                 "Thứ hai, luôn có quỹ khẩn cấp sáu tháng.",
                 "Thứ ba, đầu tư vào bản thân trước tiên.",
                 "Làm được ba điều đó bạn đã giỏi hơn chín mươi phần trăm.",
                 "Cảm ơn các bạn đã lắng nghe, hẹn gặp lại."]
        with open(os.path.join(out_dir, "test.vi-orig.vtt"), "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n")
            for i, l in enumerate(lines):
                s, e = 2 + i * 6.5, 2 + i * 6.5 + 6
                ts = lambda t: f"00:{int(t // 60):02d}:{t % 60:06.3f}"
                f.write(f"{ts(s)} --> {ts(e)}\n{l}\n\n")
        cfg = config.load()
        cfg.update({"provider": "offline", "num_clips": 2, "length": "15-30", "output_dir": os.path.join(out_dir, "out"),
                    "framing": "auto", "topic": "", "caption_preset": "hormozi", "jumpcut": True,
                    "progress_bar": True, "emphasis": True})
        result = {}
        done = threading.Event()
        job = pipeline.Job(video, cfg, log, lambda f, t: log(f"[{f:.0%}] {t}"),
                           lambda r, e: (result.update(r=r, e=e), done.set()))
        job.start()
        done.wait(1800)
        if result.get("e") or not result.get("r"):
            raise RuntimeError(f"pipeline failed: {result.get('e')}")
        clips = result["r"]["clips"]
        for c in clips:
            assert os.path.getsize(c["file"]) > 10000, c["file"]
            probe = json.loads(subprocess.run([tools._paths()["ffprobe"] if os.path.exists(tools._paths()["ffprobe"])
                                               else "ffprobe", "-v", "error", "-show_streams", "-of", "json", c["file"]],
                                              capture_output=True, text=True).stdout)
            v = next(s for s in probe["streams"] if s["codec_type"] == "video")
            assert (v["width"], v["height"]) == (1080, 1920), (v["width"], v["height"])
            # regression check: audio must be continuous from 0 s (no duplicated/jumping timestamps)
            pk = subprocess.run([tools._paths()["ffprobe"] if os.path.exists(tools._paths()["ffprobe"]) else "ffprobe",
                                 "-v", "error", "-select_streams", "a", "-show_entries", "packet=pts_time",
                                 "-of", "csv=p=0", c["file"]], capture_output=True, text=True).stdout.split()
            ts = [float(x.strip(",")) for x in pk if x.strip(",").strip()]  # first AAC packet is ~-0.021 s (priming)
            gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a > 0.1 or b - a < 0.001]
            assert ts and ts[0] < 0.05 and not gaps, f"audio timestamps broken: start={ts[:1]} bad={len(gaps)}"
            log(f"clip ok: {os.path.basename(c['file'])} {c['length']}s, audio continuous ({len(ts)} packets)")
        # the editor's live preview (framing + ASS captions + hook) must render a frame
        from app.editor import load_project, transcript_words
        from app.preview import Preview
        proj, data = load_project(result["r"]["out_dir"])
        pv = Preview(proj, transcript_words(proj))
        clip = dict(data["clips"][0], caption_style={"preset": "beast", "y": 0.6}, hook_style={"font": "Anton", "y": 0.2})
        img, handles, info = pv.render(clip, 2.0)
        assert img.size == (540, 960) and "cap" in handles and "hook" in handles, (img.size, handles)
        jc = [r.get("jump_cuts") for r in data["rendered"]]
        log(f"preview ok; jump cuts per clip: {jc}")
        # Whisper must work in the frozen app (model download + our ffmpeg decoder + ctranslate2)
        from app.engine import transcribe
        wd = os.path.join(out_dir, "whisper")
        try:
            transcribe.main([video, "--out-dir", wd, "--model", "tiny", "--language", "en"])
        except SystemExit as e:
            if e.code not in (None, 0):
                raise RuntimeError(f"whisper failed: {e.code}")
        assert os.path.exists(os.path.join(wd, "transcript.json")), "whisper wrote no transcript"
        log("whisper ok")
        # real download on this machine (informational: CI servers are sometimes blocked by YouTube)
        try:
            from app.downloader import Downloader, DownloadError
            t = tools.ensure(log)
            d = Downloader(t["yt-dlp"], t["deno"], os.path.dirname(t["ffmpeg"]), log, lambda f, s: None, lambda: False)
            v, sub, info = d.download("https://www.youtube.com/watch?v=jNQXAC9IVRw", os.path.join(out_dir, "dl"))
            log(f"download ok: {os.path.getsize(v) >> 10} KB, subs={bool(sub)}, title={info.get('title')}")
        except Exception as e:
            log(f"download check (non-fatal): {e}")
        # the window must open and close cleanly
        from app.main import App
        app = App()
        app.after(1500, app.destroy)
        app.mainloop()
        log("gui ok")
        log("SELFTEST PASSED")
        return 0
    except Exception:
        import traceback
        log(traceback.format_exc())
        log("SELFTEST FAILED")
        return 1
    finally:
        logf.close()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.getcwd(), "selftest")))
    from app.main import main
    main()
