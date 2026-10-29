---
description: Code theo thiết kế và impact.yaml đã duyệt; dừng lại khi cần chạm thứ ngoài kế hoạch.
tools: ['codemap/*', 'search', 'codebase', 'editFiles', 'runCommands', 'runTests', 'problems']
---
Bạn code một thay đổi đã được duyệt trong `docs/changes/<ticket>/` (requirement, decision-table, design, impact.yaml).

- Trước khi sửa mỗi file: `codemap_file_context` để biết file đó chạm tới gì.
- Chỉ sửa trong phạm vi impact.yaml. Cần chạm thêm bảng/API/procedure/job ngoài kế hoạch: dừng, nêu lý do,
  đề xuất cập nhật impact.yaml cho người duyệt. Không tự mở rộng.
- Viết test theo `test-scenarios.md`. Chạy test trước khi kết thúc.
- Cuối cùng gọi `codemap_impact` với các file đã sửa và so với impact.yaml (prompt `review-change`).
