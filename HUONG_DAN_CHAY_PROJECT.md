# HƯỚNG DẪN CÀI ĐẶT VÀ CHẠY PROJECT DOLA RENDER GATEWAY

Tài liệu này ghi chú lại toàn bộ cách khởi chạy, cấu hình và quản lý project dành riêng cho môi trường Windows của bạn.

## 1. Cấu hình môi trường (File `.env.local`)
Toàn bộ cấu hình hệ thống được đặt trong file `e:\dola-render-gateway\.env.local`. Nếu chưa có, hãy tạo file này.

Nội dung cơ bản của file `.env.local` hiện tại:
```ini
# Để trống proxy nếu bạn không dùng mạng ảo (VPN/Proxy)
DOLA_PROXY=

# Thời gian chờ tối đa cho 1 video (giây) - Đã tăng lên 900s (15 phút) cho Seedance 2.5
DOLA_VIDEO_TIMEOUT=900

# Số lượng video chạy song song tối đa cùng lúc (Mặc định: 3)
# Lưu ý: Cần add số tài khoản Google tương ứng vào Dashboard để chạy được số luồng này
DOLA_MAX_CONCURRENCY=5
```

## 2. Cách khởi động Server

Do chính sách Execution Policy của Windows PowerShell thường chặn script `.ps1`, bạn nên chạy trực tiếp thông qua file chạy `python.exe` trong thư mục `.venv`.

Mở PowerShell, trỏ tới thư mục chứa project và chạy lệnh sau:

```powershell
# Chuyển tới thư mục project
cd e:\dola-render-gateway

# Khởi chạy server FastAPI bằng Uvicorn
.venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000
```
*Lưu ý: Mọi thư viện cần thiết đều đã được cài sẵn trong `.venv`.*

## 3. Cách truy cập và sử dụng Dashboard

Sau khi chạy lệnh khởi động ở bước 2, server sẽ chạy ở port `8000`.

- **Mở trình duyệt và truy cập:** `http://127.0.0.1:8000/`
- Dashboard bao gồm 4 tab chính:
  1. **Generate Video:** Nơi bạn nhập Prompt, cấu hình kích thước/thời gian và tạo video trực tiếp.
  2. **Task Queue:** Xem tiến độ các video đang render, video đã xong hoặc failed. (Bao gồm các nút tiện ích như Retry, Resume, Cancel và Clear Failed Tasks).
  3. **Account List:** Quản lý tài khoản Google/Dola. Để chạy được nhiều luồng (3-5 video cùng lúc), bạn cần bấm **+ Add Account** để thêm nhiều tài khoản vào đây.
  4. **API Keys:** Quản lý khóa API nếu bạn muốn gọi tool từ tool/bot bên ngoài vào.

## 4. Cách tắt Server
Trong cửa sổ PowerShell đang chạy server, bạn chỉ cần nhấn tổ hợp phím **`Ctrl + C`** để tắt server một cách an toàn.

Nếu server bị kẹt và chạy ngầm (chạy qua Background Task), bạn có thể kiểm tra danh sách các process python đang chạy port 8000 và kill nó, hoặc đóng hẳn cửa sổ PowerShell.

## 5. Khắc phục lỗi cơ bản
- **Lỗi không mở được trình duyệt khi chạy video / add account:** Đảm bảo bạn không tắt cửa sổ trình duyệt Chromium tự động bật lên. Playwright cần nó để thao tác.
- **Lỗi task kẹt ở Processing mãi:** Tắt server (Ctrl + C) rồi chạy lại lệnh khởi động. Các task đang kẹt sẽ tự động load lại vào trạng thái để sẵn sàng Resume / Cancel trong mục Task Queue.
- **Bị lỗi Timeout quá nhiều:** Kiểm tra tab `Task Queue` xem video bị lỗi ở khâu nào. Đối với prompt quá dài chạy bằng Seedance 2.5, server đã tự động mở rộng thời gian chờ nhờ bản vá trong `video_worker_ui.py`. Mọi lỗi vặt do UI overlay (popup cookie/18+) đều đã được hệ thống tự động tắt đi bằng kịch bản quét tìm nút "OK" ngầm.
