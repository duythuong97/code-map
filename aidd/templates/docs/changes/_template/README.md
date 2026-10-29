# <TICKET>: <tên thay đổi>

Quy trình cho thay đổi liên system. Mỗi file 1–2 trang, người duyệt ký ở cuối file.

| # | File | AI làm | Người duyệt |
|---|---|---|---|
| 1 | `requirement.md`, `decision-table.md` | prompt `as-is`, `decision-table`, agent `requirement-analyst` | BA / PO |
| 2 | `impact.md`, `impact.yaml` | prompt `impact`, agent `impact-analyst` | owner từng system |
| 3 | `design.md` | đề xuất thiết kế theo system, hợp đồng interface, migration | architect + owner |
| 4 | `test-scenarios.md` | sinh từ bảng quyết định và tài liệu luồng | QA / BA |
| 5 | code | agent `implementer` | dev: diff có đúng thiết kế không |
| 6 | PR | Code Map so ảnh hưởng thực tế với `impact.yaml` | tech lead: mục ngoài kế hoạch |

Chỉ chuyển sang bước sau khi: không còn ô `?` trong bảng quyết định, mọi câu hỏi
đã đóng, mọi giả định đã được xác nhận, mỗi system bị ảnh hưởng có owner ký.
