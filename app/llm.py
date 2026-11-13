"""Clip selection with an AI model: Claude (Anthropic SDK), ChatGPT (OpenAI REST) or Gemini (Google REST)."""
import base64, io, json

import httpx

PROVIDERS = {"claude": "Claude (Anthropic)", "openai": "ChatGPT (OpenAI)", "gemini": "Gemini (Google)",
             "custom": "API tuỳ chỉnh"}
# OpenAI-compatible gateways (resellers / routers). Their keys are not Google/OpenAI keys.
GATEWAY_PRESETS = {"vilao.ai": "https://api.vilao.ai/v1", "OpenRouter": "https://openrouter.ai/api/v1"}
DEFAULT_MODELS = {
    "claude": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
    "openai": [],   # filled from the account's model list ("Tải danh sách model")
    "gemini": [],
    "custom": [],
}
# characters of transcript per request (well under each family's context window)
CHUNK_CHARS = {"claude": 600_000, "gemini": 600_000, "openai": 250_000, "custom": 250_000}

SYSTEM = """You are a senior short-form video editor (TikTok, Instagram Reels, YouTube Shorts), working like OpusClip.
From a long video you pick the moments most likely to perform as standalone vertical clips, score them, and write
the on-screen hook and the post copy. Answer with JSON only.

What makes a good clip:
1. Hook in the first 2-3 seconds: a bold claim, a question, a number, conflict, a confession - or visually the most
   striking shot. Never start on filler ("so, um", "ờ thì", greetings, sponsor reads, "subscribe").
2. Stands alone: a stranger scrolling past understands it. No dangling "that/he/như mình nói ở trên".
3. Has a payoff: ends on the answer, the punchline, the lesson, the hit. Don't cut before it, don't trail after it.
4. Talking clips start at the beginning of a sentence (use the segment start times) and end after the last word of
   the thought (a segment end time). Visual clips start and end on shot boundaries.
5. Clips never overlap.

Score each clip 0-100 as hook (0-30) + emotion (0-25) + value (0-25) + standalone (0-20). Be calibrated: most decent
clips land 55-80; keep 85+ for clips you would bet on. Fewer excellent clips beat many mediocre ones.

hook_title: the bold text card shown at the top for the first 4 seconds. <= 8 words, concrete, promises what the clip
delivers (stakes, a claim, a question, the spectacle). Don't repeat the first spoken line. No emoji, no hashtags.
title: a descriptive name for the file and report.
Captions: TikTok short and punchy (<150 chars, 1-2 emoji max); Reels a hook line + 1-3 lines of value + soft CTA;
Shorts a searchable title-like line <= 70 chars. 3-6 hashtags, Vietnamese hashtags without diacritics.
Write titles, hooks, captions and hashtags in the language the user asks for (default: the video's language).
Never invent claims the speaker didn't make."""

CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "hook_title": {"type": "string"},
                    "opening_line": {"type": "string"},
                    "score": {
                        "type": "object",
                        "properties": {k: {"type": "integer"} for k in ("total", "hook", "emotion", "value", "standalone")},
                        "required": ["total", "hook", "emotion", "value", "standalone"],
                        "additionalProperties": False,
                    },
                    "reason": {"type": "string"},
                    "caption_tiktok": {"type": "string"},
                    "caption_reels": {"type": "string"},
                    "caption_shorts": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "start", "end", "hook_title", "opening_line", "score", "reason",
                             "caption_tiktok", "caption_reels", "caption_shorts", "hashtags"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["clips", "notes"],
    "additionalProperties": False,
}

JSON_SHAPE = """Return exactly this JSON shape:
{"clips": [{"title": str, "start": seconds, "end": seconds, "hook_title": str, "opening_line": str,
  "score": {"total": int, "hook": int, "emotion": int, "value": int, "standalone": int},
  "reason": str, "caption_tiktok": str, "caption_reels": str, "caption_shorts": str, "hashtags": [str]}],
 "notes": str}"""


CLIP_TASK = {"system": SYSTEM, "schema": CLIP_SCHEMA, "shape": JSON_SHAPE}

FRAME_SYSTEM = """You are the camera operator reframing horizontal video into vertical 9:16 shorts (TikTok, Reels, Shorts).
For every numbered frame you decide how the vertical phone frame shows that shot. Answer with JSON only.
- "fill": crop a vertical window and fill the screen. Point it at what a viewer wants to see: the face of the person
  talking or reacting, a strong expression, the hands doing the action, the ball, the product. Faces beat background.
- "fit": keep the whole frame (shown smaller over a blurred copy) - only when the important things are far apart
  (two people talking across the frame, a wide action, on-screen text or a slide that must stay readable).
focus_x: horizontal centre of the vertical window, 0 = left edge, 1 = right edge (use the ruler under each frame).
zoom: 1 normal, 1.2 or 1.4 to punch in on a face or detail that is small in a wide shot (keep 1 if unsure).
focus: 2-5 words naming what you framed."""

FRAME_SCHEMA = {
    "type": "object",
    "properties": {"shots": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "layout": {"type": "string", "enum": ["fill", "fit"]},
                       "focus_x": {"type": "number"}, "zoom": {"type": "number"}, "focus": {"type": "string"}},
        "required": ["id", "layout", "focus_x", "zoom", "focus"], "additionalProperties": False}}},
    "required": ["shots"], "additionalProperties": False,
}

FRAME_TASK = {"system": FRAME_SYSTEM, "schema": FRAME_SCHEMA,
              "shape": 'Return exactly: {"shots": [{"id": int, "layout": "fill"|"fit", "focus_x": 0..1, '
                       '"zoom": 1|1.2|1.4, "focus": str}]}'}


class AIError(Exception):
    """A message that can be shown to the user as-is."""


def _fmt(t):
    m, s = divmod(float(t), 60)
    return f"{int(m):02d}:{s:04.1f}"


def build_user_text(ctx, transcript_part, part_info=""):
    o = ctx["options"]
    lines = [f"VIDEO: {ctx.get('title') or 'untitled'}"]
    if ctx.get("channel"):
        lines.append(f"Channel: {ctx['channel']}")
    lines.append(f"Duration: {ctx['duration']:.0f} s ({_fmt(ctx['duration'])}). Spoken language: {ctx.get('language') or 'unknown'}.")
    lines.append(f"Kind: {'mostly talking' if ctx['mode'] == 'talk' else 'mostly visual (little speech)'}.")
    if ctx.get("chapters"):
        lines.append("Chapters: " + "; ".join(f"{_fmt(c['start'])} {c['title']}" for c in ctx["chapters"][:60]))
    if ctx.get("description"):
        lines.append("Description (excerpt): " + ctx["description"][:800].replace("\n", " "))
    lines.append("")
    n = o["num_clips"]
    lines.append("REQUEST:")
    if n:
        lines.append(f"- Number of clips: exactly {n}. The user asked for this number - return {n} clips even if some "
                     f"are weaker (score them honestly). Only return fewer if the video physically can't hold {n} "
                     f"non-overlapping clips of this length.")
    else:
        lines.append("- Number of clips: your choice (about 1 per 5-8 minutes of video, at most 10; fewer if few "
                     "moments are strong)")
    if ctx.get("exclude"):
        lines.append("- Already chosen (do NOT overlap these ranges, in seconds): " +
                     ", ".join(f"{a:.1f}-{b:.1f}" for a, b in ctx["exclude"]))
    lines.append(f"- Clip length: {o['min_len']}-{o['max_len']} seconds")
    if o.get("topic"):
        lines.append(f"- Only moments matching this request: \"{o['topic']}\". If nothing matches, return fewer clips (even none) and say so in notes.")
    lang = {"vi": "Vietnamese", "en": "English"}.get(o.get("language"), "the video's language")
    lines.append(f"- Write titles, hooks, captions in: {lang}")
    if part_info:
        lines.append(f"- {part_info}")
    lines.append("")
    if transcript_part:
        lines.append("TRANSCRIPT (one line per segment: [#id start-end] text, times in mm:ss.s; give start/end in SECONDS):")
        lines.append(transcript_part)
    if ctx.get("highlights"):
        lines.append("")
        lines.append("MOST INTENSE STRETCHES (audio loudness + motion + cut rate; candidates, not verdicts):")
        lines.append(ctx["highlights"])
    if ctx.get("shots_txt"):
        lines.append("")
        lines.append("SHOTS in the candidate stretches (start-end, loudness):")
        lines.append(ctx["shots_txt"])
    if ctx.get("sheets"):
        lines.append("")
        lines.append("The attached images are contact sheets: one frame per shot, labelled '#id mm:ss duration'. "
                     "Use them to judge what actually happens on screen.")
    return "\n".join(lines)


def _jpeg_b64(path, max_w=1280):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def _parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise AIError("AI không trả về dữ liệu JSON hợp lệ. Thử lại hoặc đổi model.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise AIError(f"AI trả về JSON lỗi ({e}). Thử lại hoặc đổi model.")


# ---------------- Claude ----------------

# Models that accept the server-side refusal fallback ("default" form).
_CLAUDE_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


def _claude_client(key):
    import anthropic
    return anthropic.Anthropic(api_key=key, max_retries=3, timeout=900)


def _claude_call(key, model, user_text, images, task=CLIP_TASK):
    import anthropic
    client = _claude_client(key)
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}} for b in images]
    content.append({"type": "text", "text": user_text})
    base = dict(model=model, max_tokens=16000, system=task["system"], messages=[{"role": "user", "content": content}])
    structured = {"output_config": {"format": {"type": "json_schema", "schema": task["schema"]}}}
    attempts = []
    if model in _CLAUDE_FALLBACK_MODELS:
        attempts.append(("fallback", {**base, **structured}))
    attempts.append(("plain", {**base, **structured}))
    # older models without structured outputs: ask for JSON in the prompt instead
    attempts.append(("plain", {**base, "messages": [{"role": "user", "content": content[:-1] + [
        {"type": "text", "text": user_text + "\n\n" + task["shape"]}]}]}))
    last_err = None
    for kind, kwargs in attempts:
        try:
            if kind == "fallback":
                resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            last_err = e
            continue  # feature not supported by this model/account -> next, simpler attempt
        except anthropic.AuthenticationError:
            raise AIError("API key Claude không hợp lệ.")
        except anthropic.PermissionDeniedError:
            raise AIError("API key Claude không có quyền dùng model này.")
        except anthropic.NotFoundError:
            raise AIError(f"Không tìm thấy model Claude '{model}'. Bấm 'Tải danh sách model' để chọn lại.")
        except anthropic.RateLimitError:
            raise AIError("Claude đang giới hạn tốc độ (rate limit). Đợi một chút rồi thử lại.")
        except anthropic.APIStatusError as e:
            raise AIError(f"Lỗi từ Claude ({e.status_code}): {e.message}")
        except anthropic.APIConnectionError:
            raise AIError("Không kết nối được tới Claude. Kiểm tra mạng.")
        if resp.stop_reason == "refusal":
            raise AIError("Claude từ chối xử lý nội dung này. Thử model khác hoặc chế độ Offline.")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        if resp.stop_reason == "max_tokens" and not text.rstrip().endswith("}"):
            raise AIError("Câu trả lời của AI bị cắt ngắn. Giảm số clip hoặc thử lại.")
        return _parse_json(text)
    raise AIError(f"Claude báo lỗi yêu cầu: {getattr(last_err, 'message', last_err)}")


def _claude_models(key):
    import anthropic
    try:
        return [m.id for m in _claude_client(key).models.list()]
    except anthropic.AuthenticationError:
        raise AIError("API key Claude không hợp lệ.")
    except anthropic.APIConnectionError:
        raise AIError("Không kết nối được tới Claude. Kiểm tra mạng.")
    except anthropic.APIStatusError as e:
        raise AIError(f"Lỗi từ Claude ({e.status_code}): {e.message}")


# ---------------- OpenAI (ChatGPT) ----------------

def _http_error(name, r):
    try:
        msg = r.json().get("error", {})
        msg = msg.get("message") if isinstance(msg, dict) else str(msg)
    except Exception:
        msg = r.text[:300]
    if r.status_code in (401, 403):
        return AIError(f"API key {name} không hợp lệ hoặc không có quyền: {msg}")
    if r.status_code == 404:
        return AIError(f"Không tìm thấy model {name}: {msg}")
    if r.status_code == 429:
        return AIError(f"{name} báo hết hạn mức hoặc giới hạn tốc độ: {msg}")
    return AIError(f"Lỗi từ {name} ({r.status_code}): {msg}")


OPENAI_BASE = "https://api.openai.com/v1"


def _chat_call(key, model, user_text, images, base=OPENAI_BASE, name="ChatGPT", task=CLIP_TASK):
    """OpenAI chat-completions format: OpenAI itself and any compatible gateway (vilao.ai, OpenRouter...)."""
    url = base.rstrip("/") + "/chat/completions"
    SYSTEM, JSON_SHAPE = task["system"], task["shape"]
    content = [{"type": "text", "text": user_text + "\n\n" + JSON_SHAPE}]
    content += [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in images]
    body = {"model": model, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
            "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {key}"}
    try:
        r = httpx.post(url, json=body, timeout=900, headers=headers)
        if r.status_code == 400 and (images or "response_format" in r.text) and task is CLIP_TASK:
            # some gateways/models reject images or JSON mode: retry plain text, JSON asked for in the prompt
            body2 = {"model": model, "messages": [{"role": "system", "content": SYSTEM},
                                                  {"role": "user", "content": user_text + "\n\n" + JSON_SHAPE}]}
            r = httpx.post(url, json=body2, timeout=900, headers=headers)
    except httpx.HTTPError:
        raise AIError(f"Không kết nối được tới {name} ({base}). Kiểm tra mạng hoặc địa chỉ API.")
    if r.status_code != 200:
        raise _http_error(name, r)
    try:
        msg = r.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError):
        raise AIError(f"{name} trả về dữ liệu không đúng chuẩn OpenAI. Kiểm tra lại địa chỉ API (thường kết thúc bằng /v1).")
    if msg.get("refusal"):
        raise AIError(f"{name} từ chối xử lý nội dung này.")
    return _parse_json(msg.get("content"))


def _openai_call(key, model, user_text, images, task=CLIP_TASK):
    return _chat_call(key, model, user_text, images, task=task)


def _list_chat_models(key, base, name):
    try:
        r = httpx.get(base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    except httpx.HTTPError:
        raise AIError(f"Không kết nối được tới {name} ({base}). Kiểm tra mạng hoặc địa chỉ API.")
    if r.status_code != 200:
        raise _http_error(name, r)
    try:
        return r.json().get("data", [])
    except ValueError:
        raise AIError(f"{name} không trả về danh sách model. Địa chỉ API thường có dạng https://.../v1")


def _openai_models(key):
    ids = [m["id"] for m in _list_chat_models(key, OPENAI_BASE, "ChatGPT")]
    skip = ("audio", "realtime", "tts", "transcribe", "image", "search", "embedding", "instruct", "moderation",
            "whisper", "dall-e", "davinci", "babbage", "codex")
    ok = [i for i in ids if (i.startswith(("gpt-", "chatgpt-")) or (i[:1] == "o" and i[1:2].isdigit()))
          and not any(s in i for s in skip)]
    return sorted(ok, reverse=True)


# ---------------- Gemini ----------------

GEMINI = "https://generativelanguage.googleapis.com/v1beta"


def _check_google_key(key):
    if key and not key.startswith("AIza"):
        raise AIError("Key này không phải key Google Gemini (key của Google bắt đầu bằng 'AIza'). Nếu key lấy từ "
                      "cổng trung gian như vilao.ai, hãy chọn 'API tuỳ chỉnh' và nhập địa chỉ API của cổng đó "
                      "(Cài đặt → Tài khoản AI → API tuỳ chỉnh → bấm vilao.ai).")


def _gemini_call(key, model, user_text, images, task=CLIP_TASK):
    _check_google_key(key)
    parts = [{"text": user_text + "\n\n" + task["shape"]}]
    parts += [{"inlineData": {"mimeType": "image/jpeg", "data": b}} for b in images]
    body = {"systemInstruction": {"parts": [{"text": task["system"]}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json"}}
    model = model.removeprefix("models/")
    try:
        r = httpx.post(f"{GEMINI}/models/{model}:generateContent", json=body, timeout=900,
                       headers={"x-goog-api-key": key})
    except httpx.HTTPError:
        raise AIError("Không kết nối được tới Gemini. Kiểm tra mạng.")
    if r.status_code != 200:
        raise _http_error("Gemini", r)
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "không rõ")
        raise AIError(f"Gemini không trả lời (lý do: {reason}).")
    text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []) if not p.get("thought"))
    return _parse_json(text)


def _gemini_models(key):
    _check_google_key(key)
    try:
        r = httpx.get(f"{GEMINI}/models", params={"pageSize": 1000}, headers={"x-goog-api-key": key}, timeout=30)
    except httpx.HTTPError:
        raise AIError("Không kết nối được tới Gemini. Kiểm tra mạng.")
    if r.status_code != 200:
        raise _http_error("Gemini", r)
    out = []
    for m in r.json().get("models", []):
        name = m.get("name", "").removeprefix("models/")
        if "generateContent" in m.get("supportedGenerationMethods", []) and name.startswith("gemini") \
                and not any(s in name for s in ("embedding", "image", "tts", "live", "audio")):
            out.append(name)
    return sorted(out, reverse=True)


# ---------------- public API ----------------

def _custom_call(key, model, user_text, images, base=None, task=CLIP_TASK):
    if not base:
        raise AIError("Chưa nhập địa chỉ API cho 'API tuỳ chỉnh' (ví dụ https://api.vilao.ai/v1).")
    return _chat_call(key, model, user_text, images, base=base, name="API tuỳ chỉnh", task=task)


def _custom_models(key, base=None):
    if not base:
        raise AIError("Nhập địa chỉ API trước (ví dụ https://api.vilao.ai/v1).")
    data = _list_chat_models(key, base, "API tuỳ chỉnh")
    ids = [m.get("id") for m in data if m.get("id") and m.get("active", True)]
    chat = [i for i in ids if not any(x in i for x in ("embedding", "tts", "whisper", "dall-e", "image", "audio"))]
    return chat or ids


_CALL = {"claude": _claude_call, "openai": _openai_call, "gemini": _gemini_call, "custom": _custom_call}
_LIST = {"claude": _claude_models, "openai": _openai_models, "gemini": _gemini_models, "custom": _custom_models}


def list_models(provider, key, base=None):
    if not key:
        raise AIError("Nhập API key trước.")
    return _LIST[provider](key, base) if provider == "custom" else _LIST[provider](key)


def select_clips(provider, model, key, ctx, log=print, base=None):
    """Ask the model for clips. Long transcripts are split into parts and the best clips across parts kept."""
    if not key:
        raise AIError("Chưa nhập API key cho " + PROVIDERS[provider])
    if not model:
        raise AIError("Chưa chọn model. Bấm 'Tải danh sách model' rồi chọn một model.")
    transcript = ctx.get("transcript_txt", "")
    limit = CHUNK_CHARS[provider]
    lines = transcript.splitlines()
    parts, cur, size = [], [], 0
    for ln in lines:
        if size + len(ln) > limit and cur:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur or not parts:
        parts.append("\n".join(cur))
    images = [_jpeg_b64(p) for p in (ctx.get("sheets") or [])[:4]]

    all_clips, notes = [], []
    for i, part in enumerate(parts):
        info = f"This is part {i + 1} of {len(parts)} of the transcript; propose the best clips from this part." \
            if len(parts) > 1 else ""
        log(f"Đang hỏi {PROVIDERS[provider]} ({model})" + (f" - phần {i + 1}/{len(parts)}" if len(parts) > 1 else "") + "...")
        args = (key, model, build_user_text(ctx, part, info), images if i == 0 else [])
        data = _CALL[provider](*args, base=base) if provider == "custom" else _CALL[provider](*args)
        all_clips += data.get("clips") or []
        if data.get("notes"):
            notes.append(str(data["notes"]))
    return all_clips, " ".join(notes)


def frame_shots(provider, model, key, sheet_path, ids, base=None):
    """AI camera operator: one contact sheet of numbered shots -> {id: {layout, focus_x, zoom, focus}}."""
    text = ("Frames to reframe (numbers printed top-left of each frame): " + ", ".join(f"#{i}" for i in ids) +
            ". The yellow box is the current automatic crop; improve it where it misses the face, the expression "
            "or the action.")
    args = (key, model, text, [_jpeg_b64(sheet_path, max_w=1600)])
    data = _CALL[provider](*args, base=base, task=FRAME_TASK) if provider == "custom" else \
        _CALL[provider](*args, task=FRAME_TASK)
    out = {}
    for sh in data.get("shots") or []:
        try:
            out[int(sh["id"])] = {"layout": "fit" if sh.get("layout") == "fit" else "fill",
                                  "crop_x": max(0.0, min(1.0, float(sh.get("focus_x", 0.5)))),
                                  "zoom": min(1.4, max(1.0, float(sh.get("zoom") or 1.0))),
                                  "focus": str(sh.get("focus", ""))[:60]}
        except (TypeError, ValueError, KeyError):
            continue
    return out
