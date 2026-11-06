# Auto Clipper

App Windows biến video dài thành clip dọc 9:16 cho TikTok, Reels và Shorts, theo cách OpusClip làm. Dán link YouTube (hoặc chọn file), bấm **Tạo clip**, nhận về clip đã cắt, đổi khung và gắn phụ đề, kèm caption và hashtag.

Mọi bước xử lý chạy trên máy bạn: không cần gửi file lên đâu và không giới hạn dung lượng.

## Cài đặt

1. Vào mục **Releases** (hoặc **Actions → bản build mới nhất → Artifacts**) của repo này và tải `AutoClipper-Setup-x.y.z.exe`.
2. Chạy file cài. Nếu Windows SmartScreen cảnh báo, bấm **More info → Run anyway** (app chưa có chữ ký số).
3. Mở **Auto Clipper** từ Start Menu hoặc Desktop.

Lần chạy đầu, app tự tải các công cụ cần thiết (yt-dlp, ffmpeg, Deno; khoảng 200 MB, chỉ tải một lần) vào `%LOCALAPPDATA%\AutoClipper\tools`.

## Cách dùng

1. **Video**: dán link YouTube, Facebook hoặc TikTok, hoặc bấm *Chọn file...*.
2. **AI chọn đoạn hay**:
   - *Offline*: miễn phí, chọn đoạn theo âm lượng, chuyển động và câu mở đầu.
   - *Claude / ChatGPT / Gemini*: dán API key, bấm **Tải danh sách** rồi chọn model. AI chọn đoạn, chấm điểm viral, viết tiêu đề hook và caption. Bạn trả phí cho nhà cung cấp theo lượt dùng.
     - Lấy API key Claude: https://console.anthropic.com/settings/keys
     - Lấy API key ChatGPT: https://platform.openai.com/api-keys
     - Lấy API key Gemini: https://aistudio.google.com/apikey
3. **Clip**: số clip, độ dài, và ô *Chỉ lấy* để chọn theo chủ đề (ví dụ "những đoạn hài hước", "đoạn nói về marketing").
4. Bấm **Tạo clip**. Clip, ảnh bìa và `report.md` (điểm viral, caption, hashtag) được lưu vào
   `Videos\AutoClipper\<ngày>\<giờ> - <tên video>\`.
5. Mỗi clip có nút **Xem clip**, **✎ Sửa** (tiêu đề hook, phụ đề, thời lượng, âm thanh, caption), **Copy** caption và
   **Vị trí** (mở thư mục, chọn sẵn file).
6. Trang **Dự án**: xem theo ngày, tìm theo tên, xem dung lượng, đổi tên, dọn file tạm (video gốc đã tải) hoặc xoá dự án.

API key được mã hoá bằng Windows DPAPI (gắn với tài khoản Windows của bạn) và chỉ gửi tới nhà cung cấp AI bạn chọn.

## Tính năng

- Tự tải video và phụ đề từ link. Có phụ đề YouTube thì dùng luôn, không có thì tự chép lời bằng Whisper (chạy offline, hỗ trợ tiếng Việt).
- Đổi khung **theo từng cảnh**: cận cảnh thì cắt kín bám theo mặt hoặc người, cảnh rộng thì giữ nguyên khung trên nền mờ. Tự cắt viền đen có sẵn trong video.
- Thẻ **tiêu đề hook** ở đầu clip và **phụ đề tô màu từng từ** (có chế độ in hoa, chọn được màu).
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

Font Inter được dùng theo giấy phép SIL Open Font License (`assets/fonts/LICENSE-Inter.txt`).
