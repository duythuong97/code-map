# Code Map

Code Map quét source Angular, .NET (API/batch), Oracle PL/SQL và file SQL, rồi
dựng graph cho toàn hệ thống: màn hình → API → code → procedure → bảng → batch,
kèm từ điển nghiệp vụ. AI agent (GitHub Copilot, Claude Code…) đọc graph này qua
**MCP server**. Ngoài ra có thêm web UI để xem luồng và duyệt tài liệu.

Chi tiết về config, output và các lệnh nằm trong [docs/REFERENCE.md](docs/REFERENCE.md).

## Yêu cầu

- Python 3.10+
- Node.js 20+ (để quét Angular và build web UI)
- .NET SDK 9+ (chỉ cần khi quét source .NET)
- Git

## 1. Cài đặt

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm ci --prefix extractor/code_tree_exporter/extractors/angular-extractor
```

Trên Windows dùng `py -3.11 -m venv .venv` và `.venv\Scripts\python.exe` thay cho
`.venv/bin/python`.

## 2. Cấu hình

```bash
cp .env.example .env
```

Sửa 3 đường dẫn tuyệt đối trong `.env`:

| Biến | Ý nghĩa |
|---|---|
| `CODE_MAP_PROJECT_ROOT` | thư mục repo này |
| `CODE_MAP_SOURCE_ROOT` | thư mục chứa source cần quét |
| `CODE_MAP_GRAPH_OUTPUT` | thư mục output của extract (MCP server và web đọc từ đây) |

Để quét hệ thống của bạn, copy [`examples/demo-config.json`](examples/demo-config.json)
rồi sửa `root` và danh sách `sources` (mỗi repo một source, kèm `type`: `angular`,
`dotnet-api`, `dotnet-batch`, `oracle-plsql`, `sql-files`). Các tuỳ chọn khác xem
[docs/REFERENCE.md#config](docs/REFERENCE.md#config).

## 3. Extract

Chạy thử với demo có sẵn (output ra `output/demo`; demo có source .NET nên cần .NET SDK):

```bash
.venv/bin/python scripts/run_demo_pipeline.py
```

Với config của bạn:

```bash
export PYTHONPATH=extractor          # Windows: set PYTHONPATH=extractor
.venv/bin/python -m code_tree_exporter validate --config path/to/config.json
.venv/bin/python -m code_tree_exporter extract  --config path/to/config.json
```

Chạy extract lại mỗi khi code đổi. Source nào không đổi sẽ được dùng lại, nên
lần sau nhanh hơn nhiều. Để biết graph còn khớp với code hay không:

```bash
.venv/bin/python -m code_tree_exporter stale --graph <thư mục output>
```

## 4. Bật MCP cho AI agent

Kiểm tra server đọc được output:

```bash
.venv/bin/python web/backend/codemap_mcp/__main__.py --graph-output <thư mục output> --self-test
```

Lệnh in ra graph extract từ commit nào, còn khớp với code không, và danh sách tool.

### GitHub Copilot (VS Code, Agent mode)

Thêm vào `.vscode/mcp.json` của workspace (hoặc chạy lệnh **MCP: Add Server** trong
VS Code), dùng đường dẫn tuyệt đối:

```json
{
  "servers": {
    "codemap": {
      "type": "stdio",
      "command": "/path/to/code-map/.venv/bin/python",
      "args": [
        "/path/to/code-map/web/backend/codemap_mcp/__main__.py",
        "--graph-output", "/path/to/output"
      ]
    }
  }
}
```

Trên Windows: `"command": "C:\\path\\to\\code-map\\.venv\\Scripts\\python.exe"`.
Mở Copilot Chat, chọn **Agent**, rồi bật các tool `codemap_*` trong danh sách tool.

### Claude Code

```bash
claude mcp add codemap -- /path/to/code-map/.venv/bin/python \
  /path/to/code-map/web/backend/codemap_mcp/__main__.py --graph-output /path/to/output
```

### Server dùng chung cho team (HTTP)

Web backend (bước 5) phục vụ luôn MCP tại `http://<host>:8000/code-map/mcp`, đọc
`CODE_MAP_GRAPH_OUTPUT` trong `.env`:

```json
{ "servers": { "codemap": { "type": "http", "url": "http://<host>:8000/code-map/mcp" } } }
```

Với Claude Code: `claude mcp add --transport http codemap http://<host>:8000/code-map/mcp`.

### Các tool

| Tool | Dùng khi |
|---|---|
| `codemap_search` | Hỏi tự do (tiếng Việt, Nhật, Anh hoặc tên kỹ thuật) |
| `codemap_impact` | Trước khi sửa bảng, API, procedure, job; hoặc truyền danh sách file đang sửa |
| `codemap_trace_flow` | Đi theo một màn hình, API hoặc job qua các system tới dữ liệu |
| `codemap_file_context` | Một file khai báo gì và chạm tới đâu |
| `codemap_current_behavior` | Hiện trạng một chủ đề nghiệp vụ, kèm tài liệu đã duyệt |
| `codemap_check_terms` | Kiểm tra thuật ngữ và tên kỹ thuật trong một đoạn requirement |
| `codemap_status` | Graph extract từ commit nào, file nào đã cũ |

Mỗi câu trả lời mở đầu bằng một dòng cho biết graph còn khớp với code không, có
trích dẫn `file:dòng`, và kết thúc bằng mục *Not certain* (những gì phân tích
tĩnh không thấy).

## 5. Web UI (tuỳ chọn)

```bash
npm ci --prefix web/frontend && npm run build --prefix web/frontend
.venv/bin/python web/backend/api/server.py
```

Mở `http://127.0.0.1:8000/code-map/`. Muốn chạy nền thì dùng `pm2 start web/ecosystem.config.js`.

## Chạy test

```bash
.venv/bin/python -m unittest discover -s extractor/tests -t extractor
.venv/bin/python -m unittest discover -s web/tests -t .
```
