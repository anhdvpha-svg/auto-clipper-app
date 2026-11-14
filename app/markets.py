"""Target markets for organic distribution: language, time zone, local posting windows and a per-platform
checklist of the signals the feeds use to decide WHO sees a video (language, on-screen text, captions,
hashtags/keywords, posting time, account region, location tags...)."""
import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# lang = BCP-47 code used for subtitles files; lang_name = what the AI writes in
MARKETS = {
    "vn": {"label": "Việt Nam", "lang": "vi", "lang_name": "Vietnamese", "country": "Vietnam",
           "tz": "Asia/Ho_Chi_Minh", "city": "Hà Nội / TP.HCM"},
    "us": {"label": "Mỹ", "lang": "en", "lang_name": "American English", "country": "United States",
           "tz": "America/New_York", "city": "New York / Los Angeles", "tz_note": "giờ miền Đông (ET); miền Tây chậm 3 tiếng"},
    "uk": {"label": "Anh", "lang": "en", "lang_name": "British English", "country": "United Kingdom",
           "tz": "Europe/London", "city": "London"},
    "au": {"label": "Úc", "lang": "en", "lang_name": "Australian English", "country": "Australia",
           "tz": "Australia/Sydney", "city": "Sydney / Melbourne"},
    "ph": {"label": "Philippines", "lang": "en", "lang_name": "Philippine English (light Taglish is fine)",
           "country": "Philippines", "tz": "Asia/Manila", "city": "Manila"},
    "in": {"label": "Ấn Độ", "lang": "en", "lang_name": "Indian English", "country": "India",
           "tz": "Asia/Kolkata", "city": "Mumbai / Delhi"},
    "id": {"label": "Indonesia", "lang": "id", "lang_name": "Indonesian (Bahasa Indonesia, casual)",
           "country": "Indonesia", "tz": "Asia/Jakarta", "city": "Jakarta"},
    "my": {"label": "Malaysia", "lang": "ms", "lang_name": "Malay (Bahasa Melayu, casual)", "country": "Malaysia",
           "tz": "Asia/Kuala_Lumpur", "city": "Kuala Lumpur"},
    "br": {"label": "Brazil", "lang": "pt-BR", "lang_name": "Brazilian Portuguese", "country": "Brazil",
           "tz": "America/Sao_Paulo", "city": "São Paulo"},
    "mx": {"label": "Mexico / Mỹ Latinh", "lang": "es-MX", "lang_name": "Latin American Spanish (Mexico)",
           "country": "Mexico", "tz": "America/Mexico_City", "city": "Ciudad de México"},
    "es": {"label": "Tây Ban Nha", "lang": "es-ES", "lang_name": "European Spanish", "country": "Spain",
           "tz": "Europe/Madrid", "city": "Madrid"},
    "fr": {"label": "Pháp", "lang": "fr", "lang_name": "French", "country": "France", "tz": "Europe/Paris",
           "city": "Paris"},
    "de": {"label": "Đức", "lang": "de", "lang_name": "German", "country": "Germany", "tz": "Europe/Berlin",
           "city": "Berlin"},
}

# Typical high-traffic windows in the viewer's LOCAL time (published platform studies agree on these broadly;
# the account's own analytics beat them after 2-3 weeks of posting).
WINDOWS = {
    "TikTok": [(12, 14), (19, 22)],
    "Instagram Reels": [(11, 13), (19, 21)],
    "YouTube Shorts": [(12, 15), (18, 21)],
    "Facebook Reels": [(9, 11), (19, 21)],
}

CHECKLIST = {
    "TikTok": [
        "Phân phối ban đầu chủ yếu tới người dùng cùng khu vực với tài khoản (khu vực xác định lúc đăng ký và nơi "
        "đăng bài). Muốn vào thị trường nào, dùng tài khoản riêng đang hoạt động ở thị trường đó; app không giả lập "
        "vị trí.",
        "Nói / phụ đề / chữ trên màn hình bằng ngôn ngữ thị trường: TikTok nhận dạng giọng nói và chữ trong video để "
        "chọn người xem.",
        "Dòng đầu caption chứa cụm từ khoá người bản địa hay gõ khi tìm kiếm (TikTok SEO), sau đó 3–5 hashtag.",
        "Đăng vào khung giờ cao điểm theo giờ địa phương; trả lời bình luận bằng ngôn ngữ đó trong giờ đầu.",
        "Dùng âm thanh đang thịnh hành ở thị trường đó (mục Âm thanh khi đăng) nếu hợp nội dung.",
    ],
    "YouTube Shorts": [
        "Trong YouTube Studio đặt 'Ngôn ngữ video' và 'Ngôn ngữ tiêu đề & mô tả' đúng thị trường.",
        "Thêm bản dịch tiêu đề/mô tả (Studio → Phụ đề → Thêm ngôn ngữ): dùng file youtube_localizations.txt.",
        "Tải file phụ đề .srt của từng ngôn ngữ (thư mục phu_de/): YouTube dùng phụ đề để tìm kiếm và gợi ý theo "
        "ngôn ngữ người xem.",
        "Tiêu đề chứa từ khoá tìm kiếm bằng ngôn ngữ đó; đăng theo giờ cao điểm địa phương.",
    ],
    "Instagram Reels": [
        "Gắn vị trí (location tag) là một thành phố của thị trường mục tiêu.",
        "Caption và chữ trên video bằng ngôn ngữ thị trường; 3–5 hashtag ngách thay vì hashtag chung chung.",
        "Dùng 'Trial reels' (Reels thử nghiệm) để thử với người chưa theo dõi trước khi đăng chính thức.",
    ],
    "Facebook Reels": [
        "Với Trang (Page): khi đăng có thể giới hạn đối tượng theo vị trí/ngôn ngữ (Meta Business Suite → "
        "Đối tượng của bài viết).",
        "Caption bằng ngôn ngữ thị trường; đăng theo giờ địa phương.",
    ],
}


def local_tz():
    return datetime.datetime.now().astimezone().tzinfo


def posting_plan(market_key, now=None):
    """[(platform, [(local 'HH:00–HH:00', your time 'HH:00–HH:00 (+1 ngày)')])] for one market."""
    m = MARKETS[market_key]
    if ZoneInfo is None:
        return []
    try:
        tz = ZoneInfo(m["tz"])
    except Exception:
        return []
    mine = local_tz()
    today = (now or datetime.datetime.now(tz)).astimezone(tz).date()
    out = []
    for plat, wins in WINDOWS.items():
        rows = []
        for a, b in wins:
            sa = datetime.datetime.combine(today, datetime.time(a), tz).astimezone(mine)
            sb = datetime.datetime.combine(today, datetime.time(b % 24), tz).astimezone(mine)
            shift = (sa.date() - today).days
            day = {-1: " (hôm trước)", 1: " (hôm sau)"}.get(shift, "")
            rows.append((f"{a:02d}:00–{b:02d}:00", f"{sa:%H:%M}–{sb:%H:%M}{day}"))
        out.append((plat, rows))
    return out


def plan_text(market_keys):
    """Markdown: when to post, per market and platform, in market time and in the user's own time."""
    L = []
    for k in market_keys:
        m = MARKETS[k]
        plan = posting_plan(k)
        if not plan:
            continue
        note = f" ({m['tz_note']})" if m.get("tz_note") else ""
        L += ["", f"### {m['label']} · {m['country']}{note}", "",
              "| Nền tảng | Giờ địa phương | Giờ của bạn |", "|---|---|---|"]
        for plat, rows in plan:
            for loc, mine in rows:
                L.append(f"| {plat} | {loc} | {mine} |")
    return "\n".join(L)


def checklist_text():
    L = []
    for plat, items in CHECKLIST.items():
        L += ["", f"**{plat}**", ""] + [f"- {x}" for x in items]
    return "\n".join(L)


def same_language(a, b):
    return (a or "").split("-")[0].lower() == (b or "").split("-")[0].lower()
