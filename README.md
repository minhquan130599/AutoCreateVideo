# AutoCreateVideo

Dashboard tự động: **sản phẩm → kịch bản (VideoScript) → ảnh (Muse, nhiều lượt) → video (Muse)**.

## Chạy

1. Chạy VideoScript: `cd ..\VideoScript && node server.js` (mặc định http://localhost:3000).
2. Đăng nhập các tài khoản Muse trong MuseAI-API (popup "Quản lý tài khoản"). Tool đọc trực tiếp
   `..\MuseAI-API\.muse-web\accounts\` nên không cần bật MuseAI-API web khi chạy.
3. Chạy `run.bat` rồi mở http://127.0.0.1:8790 (dùng Python trong `..\MuseAI-API\.venv`).

## Quy trình mỗi sản phẩm

1. Quét `products/<tên_sản_phẩm>/*.jpg|png|webp`, chọn trực tiếp hoặc ngẫu nhiên N sản phẩm.
2. Gửi ảnh sản phẩm + tham số sang VideoScript `/api/generate-job` → N kịch bản. Lưu `output/<phiên>/<sp>/scripts.json`.
3. Mỗi kịch bản là một việc trong hàng đợi Muse, chạy trong **một đoạn chat Muse mới**:
   - Lượt 1: gửi ảnh sản phẩm + `master_image_prompt_gpt` (số cảnh + ảnh 1B, mặc định 6 ảnh).
   - Muse chỉ làm tối đa ~4 ảnh/lượt và có thể loại ảnh lỗi → tool đọc nhãn ảnh ("Ảnh 1B — …"),
     gửi tiếp tin nhắn liệt kê đúng các ảnh còn thiếu cho tới khi đủ (tối đa `Số lượt ảnh tối đa`).
   - Đủ ảnh: sắp theo thứ tự 1A, 1B, 2…N, nén lại, gửi cùng `video_prompt_muse` trong cùng đoạn chat → tải video về `output/.../vK/video/`.

## Luồng và tài khoản

Luồng i dùng tài khoản `i % số_tài_khoản`. Mỗi luồng có kết nối Muse riêng và mỗi video có đoạn chat riêng,
nên 3 luồng / 2 tài khoản → tài khoản đầu chạy song song 2 đoạn chat. "Luồng viết kịch bản" giới hạn số lần gọi VideoScript cùng lúc.

## Ghi chú

- Cấu hình lưu ở `settings.json` (có API key OpenCode — không chia sẻ file này). Để trống key thì VideoScript dùng
  `OPENCODE_API_KEY` trong `.env` của nó (nếu có).
- Tiến độ lưu ở `runs/<phiên>.json`; tắt server giữa chừng thì các mục đang chạy chuyển sang "Đã dừng",
  bấm **Chạy lại mục lỗi** để chạy tiếp (kịch bản đã đủ ảnh sẽ chỉ chạy lại bước video).
- Tránh dùng cùng tài khoản để tạo video trong MuseAI-API web khi tool đang chạy.
