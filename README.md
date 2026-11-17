# UET-LMS Notifier

Tool tự động kiểm tra **Canvas LMS (`https://portal.uet.vnu.edu.vn/`)** mỗi 30 phút bằng **GitHub Actions** và gửi email ngay khi có:
- **Bài tập / Quiz mới** (`assignment_new`)
- **Thay đổi Deadline** (`assignment_due_changed`)
- **Thông báo mới trong môn học** (`announcement`)
- **Sự kiện / Deadline mới trên Calendar** (`event_new`, `event_changed`)

Không cần cài thêm thư viện ngoài (chỉ dùng Python standard library).

---

## Hướng dẫn cài đặt (5 phút)

### Bước 1: Tạo Canvas Access Token
1. Đăng nhập `https://portal.uet.vnu.edu.vn/`
2. Vào **Tài khoản (Account) → Cài Đặt (Settings)** (`/profile/settings`)
3. Cuộn xuống mục **Tích hợp được phê duyệt (Approved Integrations)** → bấm **`+ Mã Truy Cập Mới` (`+ New Access Token`)**
4. Mục đích ghi `lms-notifier`, để trống ngày hết hạn (hoặc chọn cuối kỳ) → bấm **Tạo Mã Truy Cập**
5. **Copy ngay chuỗi Token** (chỉ hiện 1 lần).

### Bước 2: Tạo Gmail App Password (để gửi mail)
> Lưu ý: Tài khoản `@vnu.edu.vn` thường bị quản trị viên khóa tính năng App Password / SMTP. Vì vậy bạn nên dùng **1 Gmail cá nhân để gửi**, và đặt `TO_EMAIL` là **`25022948@vnu.edu.vn`** để nhận mail về mail trường.

1. Bật **Xác minh 2 bước (2-Step Verification)** trên tài khoản Gmail cá nhân của bạn.
2. Vào [Google App Passwords](https://myaccount.google.com/apppasswords), tạo một App Password mới (đặt tên `UET LMS Notifier`).
3. Copy chuỗi mật khẩu 16 ký tự (bỏ dấu cách).

### Bước 3: Đưa code lên GitHub (Private Repo)
1. Tạo một repository **Private** mới trên GitHub (ví dụ: `uet-lms-notifier`).
   *(Khuyến nghị Private; lịch chạy 30 phút/lần tiêu tốn ~1,440 phút/tháng, nằm gọn trong hạn mức 2,000 phút miễn phí/tháng của GitHub Actions).*
2. Push toàn bộ thư mục này lên repo đó:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<username>/uet-lms-notifier.git
   git push -u origin main
   ```

### Bước 4: Thêm Secrets trên GitHub
Vào repo GitHub → **Settings → Secrets and variables → Actions → New repository secret**, thêm 4 secrets:

| Name | Value |
|---|---|
| `CANVAS_TOKEN` | Token Canvas vừa tạo ở Bước 1 |
| `SMTP_USER` | Địa chỉ Gmail cá nhân dùng để gửi (ví dụ `tenban@gmail.com`) |
| `SMTP_PASS` | App Password 16 ký tự ở Bước 2 |
| `TO_EMAIL` | `25022948@vnu.edu.vn` |

*(Nếu dùng SMTP khác Gmail, thêm trong tab **Variables**: `SMTP_HOST` và `SMTP_PORT`.)*

### Bước 5: Kích hoạt & kiểm tra
1. Vào tab **Actions** trên GitHub → chọn workflow **Check UET-LMS Updates** → bấm **Run workflow**.
2. Ở lần chạy đầu tiên, tool sẽ gửi **1 email xác nhận kích hoạt** (liệt kê các môn đang theo dõi) và lưu file `state.json` vào repo để không spam lại các bài tập/thông báo cũ.
3. Từ đó trở đi, GitHub Actions tự chạy mỗi 30 phút.

---

## Chạy thử trên máy cá nhân (tùy chọn)

```powershell
$env:CANVAS_TOKEN = "<token-cua-ban>"
$env:SMTP_USER = "<gmail-gui>@gmail.com"
$env:SMTP_PASS = "<app-password-16-ky-tu>"
$env:TO_EMAIL = "25022948@vnu.edu.vn"
python main.py
```

Chạy unit test:

```powershell
python -m unittest discover -s tests -v
```
