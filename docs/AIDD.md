# AIDD với Code Map

Code Map là lớp **kiến thức và kiểm chứng** cho thay đổi liên system. Extractor
tạo ra sự thật về code. Web quản lý sự thật về nghiệp vụ do con người duyệt. MCP
đưa cả hai tới AI agent. Graph diff kiểm tra thay đổi có đúng như kế hoạch không.

```text
source repos ──extract (theo commit, incremental)──▶ graph.sqlite / codemap-rag.sqlite / codemap-web.sqlite
                                                              │
knowledge.sqlite ◀── quy tắc, quyết định, câu hỏi, tài liệu luồng (người duyệt)
                                                              │
                         web backend: REST (UI duyệt) + MCP (agent) ◀── Copilot / Claude Code / Cursor
                                                              │
                         PR: extract base + head → diff → so với impact.yaml → comment
```

## Cài cho một repo nguồn

```bash
python aidd/init.py ../order-api --server-url http://codemap.internal:8000/code-map/mcp \
    --source-type dotnet-api --system order-system --codemap-repository acme/code-map
```

Lệnh này ghi `AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`,
`.vscode/mcp.json`, `.mcp.json`, prompt file (`.github/prompts/codemap-*.prompt.md`),
custom agent (`.github/agents/*.agent.md`), template `docs/changes/_template/`, workflow
PR (`.github/workflows/codemap-pr.yml`) và `.codemap/config.json`. File đã có sẵn
được giữ nguyên (dùng `--force` để ghi đè). Bỏ `--server-url`, dùng
`--graph-output <thư mục output>` nếu muốn chạy server stdio từ checkout này.

Lưu ý:
- Tên tool trong `tools:` của custom agent (`codemap/*`) theo cú pháp của VS Code
  hiện tại; nếu phiên bản VS Code của bạn đặt tên khác, sửa trong file `.agent.md`.
- Server HTTP chỉ hiện tool ghi (`codemap_propose_*`) khi header `X-Code-Map-Token`
  đúng `CODE_MAP_KNOWLEDGE_TOKEN`. Đặt token trong `.vscode/mcp.json` (headers) cho
  những người được phép đề xuất.

## Hai làn

Mọi ticket đi qua `codemap_impact` trước:

- **Làn A (cục bộ):** ảnh hưởng nằm trong một system, không đụng bảng, batch hay
  interface dùng chung. Đi từ requirement sang code rồi review diff như bình thường.
- **Làn B (liên system):** 6 bước trong `docs/changes/<ticket>/` (xem `_template/README.md`).

| # | Bước | Prompt / agent | Người duyệt | Duyệt cái gì |
|---|---|---|---|---|
| 1 | Làm rõ requirement | `as-is`, `decision-table`, agent `requirement-analyst` | BA/PO | Bảng quyết định không còn `?`; câu hỏi đã đóng |
| 2 | Phân tích ảnh hưởng | `impact`, agent `impact-analyst` | Owner từng system | Thiếu gì, evidence có đúng không |
| 3 | Thiết kế | — | Architect + owner | Quyết định (`DEC-…`), hợp đồng interface |
| 4 | Kịch bản test | — | QA/BA | Phủ hết bảng quyết định |
| 5 | Code | agent `implementer` | Dev | Diff có đúng thiết kế không |
| 6 | PR | workflow `codemap-pr.yml` | Tech lead | Thay đổi ngoài kế hoạch |

Agent `challenger` phản biện requirement và thiết kế ở bước 1 và 3; nó chỉ đưa
ra câu hỏi, không phê duyệt.

## Kiến thức người duyệt

- Trang **Chờ duyệt** (`#/review`): quy tắc AI đề xuất, quy tắc đã duyệt nhưng
  code đã đổi, câu hỏi chờ trả lời, quyết định chờ chốt, tài liệu luồng AI viết.
- Trang **Quy tắc** (`#/knowledge`): quy tắc, quyết định, câu hỏi; tạo mới.
- Trang **KB** (`#/kb`): graph extract từ commit nào, file nào đã cũ, chất lượng
  extract, agent hỏi gì và câu nào không ra kết quả.
- Duyệt và trả lời bắt buộc có tên (ô "Tên của bạn" trên thanh trên cùng).

Quy tắc đã duyệt gắn với fingerprint hành vi của các node hiện thực nó. Lần
extract sau mà node đổi hành vi thì quy tắc tự quay về **cần duyệt lại**, và
agent thấy nhãn NEEDS REVIEW thay vì coi đó là sự thật.

## Đo chất lượng

Xem phần "Đo chất lượng của KB" trong README và
[`tools/corpus-gen/README.md`](../tools/corpus-gen/README.md). Nên bắt đầu bằng:

1. `verify-oracle` với `ALL_DEPENDENCIES` của DB thật: precision/recall PL/SQL ở mức package.
2. `corpus-gen` cỡ `m` có .NET (CI làm sẵn): recall theo từng pattern, điểm mù có được cảnh báo không.
3. Theo dõi `GET /business/kb-status` mỗi tuần: tỷ lệ câu hỏi không có kết quả,
   số quy tắc cần duyệt lại để quá lâu.
