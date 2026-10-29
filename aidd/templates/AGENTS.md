# Quy tắc cho AI agent trong repo này (AIDD với Code Map)

Repo này là một phần của hệ thống nhiều system. Kiến thức về luồng giữa các
system, bảng dữ liệu, batch và quy tắc nghiệp vụ đã được người duyệt nằm trong
**Code Map**, truy cập qua MCP server `codemap`.

## Trước khi làm

- Trước khi sửa một bảng, API, procedure, job hoặc method dùng chung: gọi
  `codemap_impact` (với `target` hoặc `files` sắp sửa). Nếu ảnh hưởng chạm từ
  **2 system trở lên**, hoặc chạm bảng/batch/interface dùng chung: **dừng lại** và
  đề nghị đi theo quy trình thay đổi liên system trong `docs/changes/` (làm rõ
  requirement → phân tích ảnh hưởng → thiết kế → test → code). Không tự code ngay.
- Trước khi viết hoặc review requirement: gọi `codemap_current_behavior` và
  `codemap_check_terms`.
- Đọc dòng đầu mỗi kết quả Code Map. Nếu KB đã cũ ở file liên quan, nói rõ và
  đọc lại code tại đó.

## Khi trả lời và viết tài liệu

- Mọi khẳng định về hành vi hiện tại phải có trích dẫn: `[N3]`, `[E2]`, `[D1]`,
  `RULE-…` hoặc `file:dòng`. Không có trích dẫn thì ghi rõ là **giả định**.
- Quy tắc nghiệp vụ chỉ được coi là đúng khi đã **approved**. Bản nháp, đề xuất
  và mục **NEEDS REVIEW** không phải sự thật đã xác nhận.
- Không tự quyết định quy tắc nghiệp vụ. Chỗ code, tài liệu và requirement lệch
  nhau, hoặc không đủ dữ liệu: tạo câu hỏi (`codemap_propose_question`) có bối
  cảnh, 2–3 phương án và hệ quả.
- "Không thấy trong graph" không có nghĩa là "không tồn tại": phân tích tĩnh
  không thấy SQL động, ORM, reflection. Luôn có mục **Không chắc**.

## Khi code

- Bám vào thiết kế và `docs/changes/<ticket>/impact.yaml` đã duyệt. Nếu thay đổi
  cần chạm thêm thứ ngoài kế hoạch: dừng và báo, không tự mở rộng phạm vi.
- PR sẽ được Code Map so ảnh hưởng thực tế với `impact.yaml`; mục ngoài kế hoạch
  sẽ bị đánh dấu.
