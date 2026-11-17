# UET-LMS Notifier

Tool tự động kiểm tra **toàn diện Canvas LMS (`https://portal.uet.vnu.edu.vn/`)** mỗi **5 phút** bằng **GitHub Actions** và gửi email ngay về `25022948@vnu.edu.vn` khi có bất kỳ thay đổi nào.

## Tính năng
1. **Kiểm tra tự động mỗi 5 phút** (`*/5 * * * *`) + hỗ trợ chạy tay (`Run workflow`).
2. **Cảnh báo KHẨN trước hạn nộp `< 2 tiếng`:**
   - Tự nhắc khi bài tập / quiz / ghi chú cá nhân / sự kiện lịch chưa hoàn thành chỉ còn `< 2 tiếng` trước deadline.
   - Nếu giảng viên mới tạo bài tập hoặc bạn mới đặt ghi chú/lịch mà hạn đã `< 2 tiếng` → báo **KHẨN ngay lập tức**.
   - Tự bỏ qua bài bạn đã nộp (`submitted` / `graded`) hoặc ghi chú đã tick hoàn thành. Nếu deadline bị dời sang giờ mới, sẽ tự nhắc lại cho giờ mới.
3. **Theo dõi toàn bộ Bảng điều khiển & Từng môn học:**
   - **Bảng điều khiển & Toàn hệ thống:** Thông báo banner toàn trường UET-LMS, Hộp thư đến (Inbox), Planner / Ghi chú cá nhân, Luồng hoạt động (`HOẠT ĐỘNG MỚI`), Mục Cần Làm (`To-Do`), Lịch cá nhân & Lịch môn học (tới 90 ngày tới).
   - **Trang chủ & Cột trái/phải từng môn:** Bài tập (mới, đổi hạn, cập nhật mô tả), Điểm thành phần & Điểm tổng kết, Phản hồi/Nhận xét bài nộp, Tập tin mới/cập nhật, Học phần (`Modules` / Slide / Tài liệu tuần / Link ngoài), Thông báo chung, Thảo luận, Câu hỏi kiểm tra (`Quizzes`), Trang (`Pages` / `Front Page`), Chương trình học (`Syllabus`).
4. **Bảo mật dữ liệu trên Public Repo:** `state.json` chỉ lưu mã băm **SHA-256** (không lưu tên môn, tên bài tập, điểm số hay nội dung ra repo).

> Lưu ý về GitHub Actions: Repo **Public** được miễn phí **không giới hạn phút** nên chạy 5 phút/lần thoải mái 24/7. Nếu đổi sang **Private repo**, GitHub miễn phí 2,000 phút/tháng (chạy 5 phút/lần tốn ~17,280 phút/tháng, nên nếu để Private bạn nên đổi cron về `*/25 * * * *` hoặc `*/30 * * * *`).

---

## Cài đặt & Chạy
1. Push code lên GitHub (`git push -u origin main`).
2. Vào **Settings → Secrets and variables → Actions → New repository secret**, thêm 4 secrets:
   - `CANVAS_TOKEN`
   - `SMTP_USER`
   - `SMTP_PASS`
   - `TO_EMAIL`
3. Vào tab **Actions → Check UET-LMS Updates → Run workflow** để kích hoạt lần đầu.

## Chạy test

```powershell
python -m unittest discover -s tests -v
```
