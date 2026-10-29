"""MCP prompts: the AIDD workflow steps as slash commands (``/mcp.codemap.as-is`` in Copilot).

Each prompt tells the agent which tools to call, what to produce and what it
must leave to people. The same texts ship as ``.github/prompts/*.prompt.md``
via ``code-tree-exporter aidd init`` for teams that prefer prompt files.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Prompt:
    name: str
    title: str
    description: str
    arguments: list[dict[str, Any]]
    template: str

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "description": self.description, "arguments": self.arguments}


COMMON = """
Quy tắc chung:
- Chỉ dùng dữ liệu từ tool codemap_* và code bạn đọc được; trích dẫn bằng id ([N3], [E2], [D1], RULE-…) hoặc file:dòng.
- Dòng đầu mỗi kết quả tool cho biết KB có cũ không. Nếu file liên quan đã đổi sau lần extract, nói rõ và đọc lại code.
- Không tự quyết định quy tắc nghiệp vụ. Mọi chỗ code, tài liệu và requirement lệch nhau, hoặc không đủ dữ liệu, là
  một câu hỏi cho người: nêu bối cảnh, 2–3 phương án và hệ quả của từng phương án, hỏi được bằng chọn một phương án.
- Mục "Không chắc" là bắt buộc, không được để trống. "Không thấy trong graph" không có nghĩa là "không tồn tại".
- Viết tiếng Việt, câu ngắn; giữ nguyên tên kỹ thuật.
"""

PROMPTS: list[Prompt] = [
    Prompt(
        "as-is",
        "Báo cáo hiện trạng",
        "Hiện trạng thực tế của một chủ đề nghiệp vụ trước khi viết requirement: luồng, quy tắc, chỗ lệch, có evidence.",
        [
            {"name": "topic", "description": "Chủ đề nghiệp vụ, ví dụ 'hủy đơn hàng'", "required": True},
            {"name": "requirement", "description": "Requirement hoặc giả định hiện có (nếu có)", "required": False},
        ],
        """Viết báo cáo hiện trạng cho chủ đề: {topic}

Requirement / giả định hiện có:
{requirement}

Các bước:
1. Gọi codemap_check_terms với chủ đề và requirement để thống nhất thuật ngữ.
2. Gọi codemap_current_behavior với chủ đề (và từng thuật ngữ nghiệp vụ tìm được).
3. Với mỗi màn hình/API/job chính, gọi codemap_trace_flow để thấy luồng qua các system.
4. Đọc code tại các file:dòng quan trọng nếu cần xác nhận điều kiện.

Kết quả (Markdown):
## Thuật ngữ
## Luồng hiện tại (theo từng system, mỗi bước có trích dẫn)
## Quy tắc đang có — bảng: quy tắc | nguồn (code/tài liệu đã duyệt/bản nháp AI) | evidence
## Chỗ lệch — bảng: requirement/giả định nói | code/tài liệu đang làm | evidence | câu hỏi cần quyết định
## Câu hỏi cho BA/SME (đóng, có phương án)
## Không chắc
""" + COMMON,
    ),
    Prompt(
        "decision-table",
        "Requirement thành bảng quyết định",
        "Chuyển requirement dạng văn xuôi thành bảng quyết định / bảng chuyển trạng thái; đánh dấu ô '?' cần người quyết.",
        [{"name": "requirement", "description": "Đoạn requirement cần chuyển", "required": True}],
        """Chuyển requirement sau thành bảng quyết định:

{requirement}

Các bước:
1. Gọi codemap_check_terms với requirement.
2. Gọi codemap_current_behavior cho từng thực thể/trạng thái được nhắc tới để biết các giá trị thật
   (trạng thái, cờ, loại) đang có trong code và database.

Kết quả:
## Điều kiện và giá trị (lấy giá trị thật từ code/DB, có trích dẫn)
## Bảng quyết định — mỗi tổ hợp điều kiện một dòng; ô requirement không nói tới ghi **?**; không tự điền
## Bảng chuyển trạng thái (nếu có vòng đời)
## Ví dụ cụ thể (Given/When/Then với dữ liệu thật) cho các dòng chính
## Câu hỏi cho từng ô '?' (đóng, có phương án và hiện trạng code)
## Không chắc
""" + COMMON,
    ),
    Prompt(
        "impact",
        "Phân tích ảnh hưởng",
        "Phân tích ảnh hưởng của một thay đổi qua mọi system, nhóm theo system/owner, ghi thành impact.yaml để đối chiếu sau khi code.",
        [
            {"name": "change", "description": "Mô tả thay đổi (requirement đã chốt hoặc thiết kế)", "required": True},
            {"name": "targets", "description": "Bảng/API/procedure/file dự kiến sửa, cách nhau bằng dấu phẩy", "required": False},
        ],
        """Phân tích ảnh hưởng cho thay đổi:

{change}

Đối tượng dự kiến sửa: {targets}

Các bước:
1. Gọi codemap_impact cho từng đối tượng (target) và với danh sách file dự kiến sửa (files).
2. Với mỗi bảng bị ghi, gọi codemap_impact với bảng đó để thấy mọi chỗ đọc/ghi khác (batch, báo cáo, system khác).
3. Gọi codemap_current_behavior cho chủ đề để biết quy tắc đã duyệt bị ảnh hưởng.

Kết quả:
## Tóm tắt (system nào, mức độ)
## Ảnh hưởng theo system — mỗi system: thành phần phải sửa | thành phần phải test lại | owner cần review
## Dữ liệu — bảng bị ghi/đọc, dữ liệu cũ cần chuyển đổi
## Batch và lịch chạy liên quan
## Quy tắc nghiệp vụ bị ảnh hưởng (RULE-…/[D…])
## Không chắc (bắt buộc)
## impact.yaml — khối YAML để lưu vào docs/changes/<ticket>/impact.yaml:
```yaml
change: <id ngắn>
expected:
  # mỗi mục: kind (node|table|api|file), name (tên kỹ thuật như trong graph), action (modify|read|write|call|test)
  - {{kind: table, name: ORDER_HEADER, action: write}}
uncertain:
  - <mục chưa chắc>
```
""" + COMMON,
    ),
    Prompt(
        "review-requirement",
        "Phản biện requirement",
        "Đóng vai phản biện từ nhiều góc nhìn (vận hành, kế toán, batch, dữ liệu cũ, đồng thời); chỉ đưa ra câu hỏi, không phê duyệt.",
        [{"name": "requirement", "description": "Requirement hoặc thiết kế cần phản biện", "required": True}],
        """Phản biện requirement/thiết kế sau. Bạn KHÔNG phê duyệt hay kết luận đạt/không đạt; bạn chỉ tìm câu hỏi.

{requirement}

Các bước:
1. Gọi codemap_check_terms với toàn bộ văn bản.
2. Gọi codemap_current_behavior và codemap_impact cho các thực thể chính.
3. Đi qua checklist: dữ liệu cũ đang có; thời điểm (ngày chốt, ngày làm việc, giờ batch, múi giờ); đồng thời
   (hai người, batch chạy cùng lúc); chạy lại/idempotent; phân quyền; audit; báo cáo; interface với system ngoài;
   rollback; lỗi và ngoại lệ; hiệu năng với khối lượng thật.

Kết quả:
## Mâu thuẫn với quy tắc hoặc hiện trạng (có trích dẫn)
## Trường hợp còn thiếu (theo từng mục checklist)
## Thuật ngữ mơ hồ hoặc tên kỹ thuật không tồn tại
## Câu hỏi cho người (đóng, có phương án), xếp theo mức rủi ro
## Không chắc
""" + COMMON,
    ),
    Prompt(
        "document-flow",
        "Viết tài liệu luồng nghiệp vụ",
        "Viết nháp tài liệu cho một luồng (tóm tắt, bước, quy tắc có evidence, edge case, câu hỏi) và gửi vào hàng chờ duyệt.",
        [{"name": "entry", "description": "Màn hình, UI action, API hoặc job bắt đầu luồng", "required": True}],
        """Viết tài liệu nghiệp vụ cho luồng bắt đầu từ: {entry}

Các bước:
1. Gọi codemap_trace_flow với entry, rồi codemap_current_behavior với tên nghiệp vụ của luồng.
2. Đọc code tại các điều kiện quan trọng để lấy quy tắc (IF/CASE/EXCEPTION) kèm file:dòng.
3. Nếu có tool codemap_propose_flow_doc, gửi tài liệu vào hàng chờ duyệt bằng tool đó (không tự đánh dấu là đã duyệt).
   Nếu không có, trả về Markdown theo cấu trúc bên dưới.

Cấu trúc: tên luồng (động từ + đối tượng); tóm tắt 1–3 đoạn (ai, để làm gì, kết quả, không bao gồm gì); mỗi bước theo
system: tên, người/hệ thống thực hiện, mô tả có trích dẫn; quy tắc (câu khẳng định + evidence file:dòng + ok/warn/bad);
edge case; câu hỏi mở.
""" + COMMON,
    ),
    Prompt(
        "review-change",
        "Review thay đổi theo kế hoạch",
        "So thay đổi code (diff hoặc danh sách file) với impact.yaml đã duyệt; chỉ ra ảnh hưởng ngoài kế hoạch.",
        [
            {"name": "files", "description": "Các file đã sửa, cách nhau bằng dấu phẩy", "required": True},
            {"name": "plan", "description": "Nội dung impact.yaml đã duyệt (nếu có)", "required": False},
        ],
        """Review thay đổi: các file đã sửa {files}

Kế hoạch ảnh hưởng đã duyệt:
{plan}

Các bước:
1. Gọi codemap_impact với files.
2. Đối chiếu với kế hoạch: liệt kê thành phần bị chạm mà kế hoạch không có, và mục trong kế hoạch mà thay đổi không chạm.
3. Gọi codemap_current_behavior cho các quy tắc liên quan để kiểm tra code mới còn đúng quy tắc đã duyệt.

Kết quả:
## Ngoài kế hoạch (cần người quyết)
## Trong kế hoạch nhưng chưa thấy thay đổi
## Quy tắc đã duyệt có thể bị ảnh hưởng
## Owner cần review
## Không chắc
""" + COMMON,
    ),
]


def render_prompt(prompt: Prompt, arguments: dict[str, str]) -> dict[str, Any]:
    missing = [argument["name"] for argument in prompt.arguments if argument.get("required") and not arguments.get(argument["name"])]
    if missing:
        raise ValueError(f"missing argument(s): {', '.join(missing)}")
    values = {argument["name"]: arguments.get(argument["name"], "") or "(không có)" for argument in prompt.arguments}
    return {
        "description": prompt.description,
        "messages": [{"role": "user", "content": {"type": "text", "text": prompt.template.format(**values).strip()}}],
    }
