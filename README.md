# Auto Clipper

App Windows biến video dài thành clip dọc 9:16 cho TikTok, Reels và Shorts, theo cách OpusClip làm. Dán link YouTube (hoặc chọn file), bấm **Tạo clip**, nhận về clip đã cắt, đổi khung và gắn phụ đề, kèm caption và hashtag.

Mọi bước xử lý chạy trên máy bạn: không cần gửi file lên đâu và không giới hạn dung lượng.

## Cài đặt

1. Vào mục **Releases** (hoặc **Actions → bản build mới nhất → Artifacts**) của repo này và tải `AutoClipper-Setup-x.y.z.exe`.
2. Chạy file cài. Nếu Windows SmartScreen cảnh báo, bấm **More info → Run anyway** (app chưa có chữ ký số).
3. Mở **Auto Clipper** từ Start Menu hoặc Desktop.

Lần chạy đầu, app tự tải các công cụ cần thiết (yt-dlp, ffmpeg, Deno; khoảng 200 MB, chỉ tải một lần) vào `%LOCALAPPDATA%\AutoClipper\tools`.

## Cách dùng

1. **Video**: dán link YouTube, TikTok, Instagram, Facebook, Twitch..., hoặc bấm *Tải file lên*.
   Bài Instagram/TikTok giới hạn đối tượng cần đăng nhập: app tự thử lấy đăng nhập từ trình duyệt trên máy; hoặc chọn
   trình duyệt / file `cookies.txt` trong **Cài đặt → Tải video**.
2. **AI chọn đoạn hay**:
   - *Offline*: miễn phí, chọn đoạn theo âm lượng, chuyển động và câu mở đầu.
   - *Claude / ChatGPT / Gemini*: dán API key, bấm **Tải danh sách** rồi chọn model. AI chọn đoạn, chấm điểm viral, viết tiêu đề hook và caption. Bạn trả phí cho nhà cung cấp theo lượt dùng.
     - Lấy API key Claude: https://console.anthropic.com/settings/keys
     - Lấy API key ChatGPT: https://platform.openai.com/api-keys
     - Lấy API key Gemini: https://aistudio.google.com/apikey
3. **Clip**: số clip, độ dài, và ô *Chỉ lấy* để chọn theo chủ đề (ví dụ "những đoạn hài hước", "đoạn nói về marketing").
4. Bấm **Tạo clip**. Clip, ảnh bìa và `report.md` (điểm viral, caption, hashtag) được lưu vào
   `Videos\AutoClipper\<ngày>\<giờ> - <tên video>\`.
5. Mỗi clip có nút **Xem clip**, **✎ Sửa**, **Copy** caption và **Vị trí** (mở thư mục, chọn sẵn file).
   Trình **✎ Sửa** có khung xem trước trực tiếp: kéo tiêu đề/phụ đề để di chuyển, lăn chuột để đổi cỡ, kéo ngang hình để
   dời khung dọc của từng cảnh, nhấp đúp để đổi Lấp đầy ↔ Nền mờ, nút zoom 1x/1.2x/1.4x; 10 kiểu chữ phụ đề, 6 phông và
   7 kiểu khung tiêu đề.
6. Trang **Dự án**: xem theo ngày, tìm theo tên, xem dung lượng, đổi tên, dọn file tạm (video gốc đã tải) hoặc xoá dự án.

API key được mã hoá bằng Windows DPAPI (gắn với tài khoản Windows của bạn) và chỉ gửi tới nhà cung cấp AI bạn chọn.

## Thị trường mục tiêu (bản địa hoá, phân phối organic)

Nền tảng chọn người xem dựa trên ngôn ngữ nói/phụ đề/chữ trên video, caption và từ khoá, hashtag, giờ đăng, khu vực
của tài khoản và vị trí gắn kèm. Ở trang Tạo clip, chọn nước muốn nhắm tới (Mỹ, Anh, Úc, Philippines, Ấn Độ,
Indonesia, Malaysia, Brazil, Mexico/Mỹ Latinh, Tây Ban Nha, Pháp, Đức, Việt Nam). Với AI, mỗi clip có thêm 1 bản cho
mỗi nước:

- Phụ đề viết lại bằng tiếng bản địa (giữ đúng thời gian; đổi tiền tệ/đơn vị sang thứ người xem hiểu), tuỳ chọn
  **song ngữ** (dòng gốc nhỏ bên dưới).
- Tiêu đề hook, caption TikTok/Reels/Shorts, hashtag người bản địa theo dõi, từ khoá tìm kiếm (dòng đầu caption).
- File phụ đề `.srt` từng ngôn ngữ (`phu_de/`) và `youtube_localizations.txt` (tiêu đề/mô tả dịch cho YouTube Studio).
- **Lịch đăng** theo giờ cao điểm của từng nước, đổi sang giờ máy của bạn, và checklist tín hiệu phân phối trong
  `report.md`.

Lưu ý: TikTok phân phối trước hết tới khu vực của tài khoản; nên dùng tài khoản riêng đang hoạt động ở thị trường đó.
App không giả lập vị trí.

## Khám phá nguồn (tìm video để clip)

- **Chọn nền tảng để quét**: YouTube video dài, YouTube Shorts, TikTok, Instagram Reels, Twitch, Kick, Dailymotion,
  Bilibili, X (Twitter), Rumble. Nhiều từ khoá cách nhau bằng dấu phẩy.
  - TikTok/Instagram tìm theo hashtag và cần đăng nhập (trình duyệt hoặc `cookies.txt`).
  - Twitch/Kick/X/Rumble không có tìm kiếm công khai: gõ tên kênh, app lấy clip hot 7 ngày + VOD mới (Twitch).
- **Kênh theo dõi**: dán link kênh của các creator trong campaign; app gom video mới nhất của tất cả các kênh.
- **Bộ lọc viral**: lượt xem tối thiểu, view/giờ tối thiểu, điểm Hot tối thiểu, loại trừ từ, chỉ video **vượt trội**
  (nhiều view gấp ≥2 lần mức thường của kênh), sắp xếp theo Hot / view/giờ / vượt trội / lượt xem / mới nhất.
- Mỗi video có **điểm Hot** (tốc độ tăng view mỗi giờ, độ mới, độ dài phù hợp để cắt, tương tác, mức vượt trội) và nhãn
  *Vượt trội x5*, *Đang lên*, *Mới*, *Dài*, *Đã cắt*.
- **▶ Xem** ngay trong app (không cần mở trình duyệt): video, lượt xem/thích, **caption / mô tả** và **lời thoại** của
  video, có nút Copy caption / Copy hashtag / Copy link.
- **🔖 Lưu** link vào **Kho link**, chia theo dự án / campaign: xem lại, copy tất cả link, cắt cả dự án một lần,
  dán thêm link bằng tay.
- **Đăng nhập nền tảng ngay trong app** (TikTok, Instagram, X, Facebook, Twitch, YouTube): nút 🔒 mở cửa sổ đăng
  nhập, đăng nhập như trên web, app nhớ phiên cho mọi lần quét và tải sau. Nền tảng cần đăng nhập có dấu 🔒 trên nút chọn.
- Tick nhiều video → **Thêm vào hàng chờ**: app tự tải và cắt lần lượt với cài đặt ở trang Tạo clip.

## Tính năng

- Tự tải video và phụ đề từ link. Có phụ đề YouTube thì dùng luôn, không có thì tự chép lời bằng Whisper (chạy offline, hỗ trợ tiếng Việt).
- Đổi khung **theo từng cảnh** bằng bộ nhận diện khuôn mặt YuNet: bám người đang nói (nhận ra miệng đang mấp máy),
  lia theo mặt di chuyển, tự zoom 1.2–1.4x vào mặt nhỏ ở cảnh rộng, không có người thì bám vào chỗ có hành động; cảnh
  có nhiều người đứng xa nhau thì giữ nguyên khung trên nền mờ. Tự cắt viền đen có sẵn trong video.
- **AI chọn khung hình** (khi dùng Claude/ChatGPT/Gemini/API): AI xem ảnh từng cảnh để chọn mặt, biểu cảm hoặc hành động cần bám.
- **Nhịp dựng giữ chân người xem**: cắt khoảng lặng và từ đệm ("ừm", "à", "uh"), tô màu từ khoá (số, tiền, từ mạnh),
  thanh tiến trình dưới đáy video.
- Thẻ **tiêu đề hook** ở đầu clip và **phụ đề tô màu từng từ** với 10 kiểu chữ trend: Cổ điển vàng, Hormozi, MrBeast,
  Hộp màu, Neon, Karaoke, Từng chữ, Tối giản, Nền đen, Viết tay.
- Âm lượng chuẩn hoá -14 LUFS, giữ nguyên fps gốc (tối đa 60), kết thúc bằng màn đen.
- Chọn đoạn theo chủ đề, giống tính năng ClipAnything.

## Lưu ý

- Chỉ cắt clip từ video bạn có quyền sử dụng (kênh của bạn hoặc đã được cho phép).
- Nếu YouTube bắt đăng nhập ("not a bot"), hãy mở YouTube bằng trình duyệt trên máy và đăng nhập, rồi thử lại. Có thể tải video bằng cách khác rồi dùng *Chọn file...*.
- Lần đầu dùng Whisper, app tải model nhận dạng giọng nói (khoảng 500 MB với model `small`).

## Build từ mã nguồn

```bash
pip install -r requirements.txt pyinstaller
python build.py                         # -> dist/AutoClipper/
dist/AutoClipper/AutoClipper --selftest selftest   # tự kiểm tra toàn bộ quy trình
```

Bộ cài Windows được build tự động bằng GitHub Actions (`.github/workflows/build.yml`) mỗi khi đẩy code lên nhánh `main`, hoặc tạo tag `v*` để phát hành bản Release.

Phần xử lý trong `app/engine/` dùng chung với skill `auto-clipper` của Claude.

Font Inter, Anton, Bangers, Be Vietnam Pro, Paytone One và Patrick Hand được dùng theo giấy phép SIL Open Font License
(`assets/fonts/LICENSE-*.txt`). Model nhận diện khuôn mặt YuNet (OpenCV Zoo) theo giấy phép MIT
(`assets/models/LICENSE-yunet.txt`).
