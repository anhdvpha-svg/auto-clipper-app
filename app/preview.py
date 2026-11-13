"""Live preview for the clip editor: one frame of the finished clip at any time t, composed the same way the
renderer does it (framing, captions in the chosen typography, hook card, progress bar), in ~0.1-0.3 s."""
import json, os, shutil, subprocess

from PIL import Image, ImageFilter

from .engine import render_clips as R

W, H = 1080, 1920          # output geometry (coordinates used by styles and drag handles)
PW, PH = 540, 960          # preview image size
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _arg(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


class Preview:
    def __init__(self, proj, words):
        self.proj, self.words = proj, words
        argv = proj.get("base_args") or []
        self.video = proj["video"]
        info = R.ffprobe_json(self.video)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.src_w, self.src_h = int(v["width"]), int(v["height"])
        if str((v.get("tags") or {}).get("rotate", "0")) in ("90", "270", "-90"):
            self.src_w, self.src_h = self.src_h, self.src_w
        self.duration = float(info["format"].get("duration") or 0)
        self.shots, self.act = None, {"x": 0, "y": 0, "w": self.src_w, "h": self.src_h}
        sp = _arg(argv, "--shots")
        if sp and os.path.exists(sp):
            sj = json.load(open(sp, encoding="utf-8"))
            self.shots = sj.get("shots")
            self.act = sj.get("active_area") or self.act
        self.reframe = _arg(argv, "--reframe") or ("auto" if self.shots else "center")
        self.defaults = {"preset": _arg(argv, "--caption-preset", "classic"),
                         "highlight": _arg(argv, "--highlight") if "--highlight" in argv else None,
                         "uppercase": True if "--uppercase" in argv else None,
                         "emphasis": "--emphasis" in argv, "jumpcut": "--jumpcut" in argv,
                         "progress_bar": "--progress-bar" in argv, "captions": "--no-captions" not in argv,
                         "hook": "--no-hook" not in argv}
        base = proj.get("work") if os.path.isdir(proj.get("work") or "") else proj["out_dir"]
        self.dir = os.path.join(base, "_preview")
        os.makedirs(self.dir, exist_ok=True)
        fd = R.fonts_dir()
        self.has_fonts = False
        if fd and os.path.isdir(fd):
            local = os.path.join(self.dir, "_fonts")
            os.makedirs(local, exist_ok=True)
            for fn in os.listdir(fd):
                if fn.lower().endswith((".otf", ".ttf")) and not os.path.exists(os.path.join(local, fn)):
                    shutil.copy(os.path.join(fd, fn), local)
            self.has_fonts = True
        self._frames = {}

    # ---------- timing ----------
    def ranges(self, clip):
        rs = R.clip_ranges(clip, self.words, self.duration)
        if clip.get("jumpcut", self.defaults["jumpcut"]) and self.words:
            rs = R.jumpcut_ranges(rs, self.words)
        return [tuple(r) for r in rs]

    @staticmethod
    def src_time(ranges, t):
        off = 0.0
        for s, e in ranges:
            if t < off + (e - s) or (s, e) == ranges[-1]:
                return min(e - 0.02, s + max(0.0, t - off))
            off += e - s
        return ranges[-1][1]

    def segment_at(self, clip, ranges, src_t):
        segs = R.plan_segments(ranges, clip, self.reframe, self.shots)
        for sg in segs:
            if sg[0] <= src_t < sg[1]:
                return sg
        return segs[-1] if segs else (src_t, src_t + 1, "fill", 0.5, {})

    def shot_at(self, src_t):
        for sh in self.shots or []:
            if sh["start"] <= src_t < sh["end"]:
                return sh
        return None

    # ---------- pictures ----------
    def frame(self, src_t):
        key = round(src_t, 1)
        if key in self._frames:
            return self._frames[key]
        a = self.act
        crop = f"crop={a['w']}:{a['h']}:{a['x']}:{a['y']}," if (a["w"], a["h"]) != (self.src_w, self.src_h) else ""
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{key:.2f}", "-i", self.video, "-frames:v", "1",
                            "-vf", f"{crop}scale='min(1280,iw)':-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                           capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        import io
        try:
            im = Image.open(io.BytesIO(r.stdout)).convert("RGB")
        except Exception:
            im = Image.new("RGB", (1280, 720), (20, 20, 20))
        if len(self._frames) > 40:
            self._frames.pop(next(iter(self._frames)))
        self._frames[key] = im
        return im

    def compose_video(self, img, seg, src_t):
        """The framed picture (no text) at preview size."""
        _, _, lay, cx, ex = seg
        aw, ah = self.act["w"], self.act["h"]
        sf = img.width / aw
        if aw / ah <= W / H + 0.01:
            return img.resize((PW, int(img.height * PW / img.width))).crop((0, 0, PW, PH))
        if lay == "fit":
            bg = img.resize((PW // 4, PH // 4)).filter(ImageFilter.GaussianBlur(3)).resize((PW, PH))
            bg = Image.eval(bg, lambda v: int(v * 0.86))
            fh = int(img.height * PW / img.width)
            bg.paste(img.resize((PW, fh)), (0, (PH - fh) // 2))
            return bg
        z = ex.get("zoom", 1.0) if aw / ah > W / H * 1.2 else 1.0
        cw, ch = R.crop_geometry(aw, ah, W, H, z)
        tr = ex.get("track")
        if tr:
            pts = sorted(tr)
            cx = pts[0][1] if src_t <= pts[0][0] else pts[-1][1]
            for (t1, x1), (t2, x2) in zip(pts, pts[1:]):
                if t1 <= src_t < t2 and t2 > t1:
                    cx = x1 + (x2 - x1) * (src_t - t1) / (t2 - t1)
        x, y = R.crop_xy(cx, ex.get("crop_y", 0.5), cw, ch, aw, ah)
        box = (int(x * sf), int(y * sf), int((x + cw) * sf), int((y + ch) * sf))
        return img.crop(box).resize((PW, PH), Image.BICUBIC)

    def render(self, clip, t, cap_chunks=None):
        """-> (PIL image PW x PH, handles {"hook": box, "cap": box} in output pixels, info)."""
        ranges = self.ranges(clip)
        total = sum(e - s for s, e in ranges)
        t = max(0.0, min(t, total - 0.05))
        src_t = self.src_time(ranges, t)
        seg = self.segment_at(clip, ranges, src_t)
        img = self.compose_video(self.frame(src_t), seg, src_t)
        handles = {}
        cs = clip.get("caption_style") or {}
        D = self.defaults
        if cs.get("enabled", D["captions"]):
            preset = cs.get("preset", D["preset"])
            P = R.caption_preset(preset)
            words, _ = R.clip_words_of(ranges, self.words)
            if words or cap_chunks:
                hl = cs.get("highlight") or D["highlight"]
                up = cs.get("uppercase", D["uppercase"])
                ass = R.build_ass(words, W, H, 4, hl.lstrip("#") if hl else None, up, size=float(cs.get("size", 1.0)),
                                  y=float(cs.get("y", 0.75)), chunks=cap_chunks, preset=preset,
                                  emphasis=cs.get("emphasis", D["emphasis"]))
                open(os.path.join(self.dir, "p.ass"), "w", encoding="utf-8").write(ass)
                img.save(os.path.join(self.dir, "base.png"))
                fonts = ":fontsdir=_fonts" if self.has_fonts else ""
                r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", "base.png", "-vf",
                                    f"setpts=PTS+{t:.3f}/TB,ass=p.ass{fonts}", "-frames:v", "1", "out.png"],
                                   cwd=self.dir, capture_output=True, stdin=subprocess.DEVNULL,
                                   creationflags=_NO_WINDOW)
                if r.returncode == 0:
                    img = Image.open(os.path.join(self.dir, "out.png")).convert("RGB")
                fs = W * 0.072 * float(cs.get("size", 1.0)) * P.get("size", 1.0)
                yb = float(cs.get("y", 0.75)) * H
                handles["cap"] = (70, yb - fs * 1.35, W - 70, yb + fs * 0.2)
        hs = clip.get("hook_style") or {}
        text = (clip.get("hook_title") or "").strip()
        if text and hs.get("enabled", D["hook"]):
            secs = hs.get("seconds", 4.0)
            shown = secs in (None, "all", 0) or t < float(secs)
            path = os.path.join(self.dir, "hook.png")
            accent = (cs.get("highlight") or D["highlight"] or R.caption_preset(cs.get("preset", D["preset"]))["hl"])
            if R.make_hook_png(text, W, path, uppercase=hs.get("uppercase", True), size=float(hs.get("size", 1.0)),
                               theme=hs.get("theme", "light"), accent=accent.lstrip("#"), font=hs.get("font", "Inter")):
                card = Image.open(path)
                y = R.hook_y(hs, card.height, H)
                handles["hook"] = ((W - card.width) / 2, y, (W + card.width) / 2, y + card.height)
                if shown:
                    small = card.resize((card.width // 2, card.height // 2))
                    img.paste(small, ((PW - small.width) // 2, y // 2), small)
        if clip.get("progress_bar", D["progress_bar"]) and total > 3:
            from PIL import ImageDraw
            col = "#" + ((cs.get("highlight") or D["highlight"] or "FFE600").lstrip("#"))
            ImageDraw.Draw(img).rectangle([0, PH - 6, int(PW * t / total), PH], fill=col)
        info = {"src_t": src_t, "seg": seg, "total": total, "shot": self.shot_at(src_t)}
        return img, handles, info
