# Code Map

Code Map quét source Angular, .NET (API/batch), Oracle PL/SQL và file SQL, rồi
dựng graph cho toàn hệ thống: màn hình → API → code → procedure → bảng → batch,
kèm từ điển nghiệp vụ. AI agent (GitHub Copilot, Claude Code…) đọc graph này qua
**MCP server**. Ngoài ra có thêm web UI để xem luồng và duyệt tài liệu.

Chi tiết về config, output và các lệnh nằm trong [docs/REFERENCE.md](docs/REFERENCE.md);
kiến trúc nằm trong [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Mọi việc đi qua một lệnh: `codemap.cmd` trên Windows, `./codemap.sh` trên
macOS/Linux (`--help` để xem danh sách lệnh).

## Yêu cầu

- Python 3.10+
- Node.js 20+ (để quét Angular và build web UI)
- .NET SDK 9+ (chỉ cần khi quét source .NET)
- Git hoặc Subversion (`svn` command line; trên Windows là "command line client tools" của TortoiseSVN) cho repo cần quét

## 1. Cài đặt

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm ci --prefix extractor/code_tree_exporter/extractors/angular-extractor
```

Chạy ở thư mục gốc của repo: `requirements.txt` cài luôn chính repo này ở chế độ
editable (`pip install -e .`), nên `codemap`, `web` và `code_tree_exporter` import
được từ bất kỳ đâu, không cần `PYTHONPATH`. Trên Windows dùng `py -3.11 -m venv .venv`
và `.venv\Scripts\python.exe` thay cho `.venv/bin/python`.

## 2. Cấu hình

Demo chạy ngay, không cần cấu hình gì. Với hệ thống thật, mọi đường dẫn nằm trong
**một file config** đặt ngoài repo này. Extractor, web và MCP cùng đọc file đó:

```text
D:/code-map-prod/
├── code-map.json     ← tạo bằng codemap.cmd init (dưới đây)
├── input-data/       ← CSV nghiệp vụ (từ điển, jobnet, ...)
├── catalog/
└── output/           ← extract ghi vào đây; web và MCP đọc từ đây
```

Tạo config bằng cách quét mọi repo trong một thư mục:

```bash
codemap.cmd init --root C:/sources --config D:/code-map-prod/code-map.json --database DB1 --schema APP
# ./codemap.sh init ... trên macOS/Linux; thêm --dry-run để xem trước, không ghi file
```

Lệnh đi vào từng repo trong `C:/sources` (repo git hoặc working copy SVN; kể cả
thư mục nhóm chứa nhiều repo) và thêm một source cho mỗi loại code tìm thấy:
`angular` (`angular.json`, hoặc `.angular-cli.json` của Angular 2–5), `dotnet-api`/`dotnet-batch` (`.csproj` web hoặc Exe; bỏ qua
thư viện và project test), `oracle-plsql` (`.pkb`, `.pks`, … hoặc `.sql` có
`CREATE PACKAGE/PROCEDURE/...`), `sql-files` (`.sql` khác, `.ctl`), `api-gateway`
(template CloudFormation có `AWS::ApiGateway::Method`). Repo không
có gì để quét thì được liệt kê để bạn biết. Chạy lại khi có repo mới: source đã
có (và những gì bạn đã sửa trong đó) được giữ nguyên, chỉ thêm repo mới.

Sau đó xem lại `code-map.json`: `database`/`schema` của từng source. Repo không
cần quét thì xoá source của nó và thêm tên repo vào `ignoreRepositories`, để lần
`init` sau không thêm lại. Đường dẫn tương đối được tính từ file config. Các tuỳ chọn khác xem
[docs/REFERENCE.md#config](docs/REFERENCE.md#config).

Rồi trỏ `.env` tới file đó:

```bash
cp .env.example .env
# CODE_MAP_CONFIG=D:/code-map-prod/code-map.json   (Windows: dùng / để khỏi escape)
# CODE_MAP_KNOWLEDGE_TOKEN=...                      (để lưu ghi chú trên web)
```

## 3. Extract

Validate rồi extract trong một lệnh, với config trong `CODE_MAP_CONFIG`
(chưa đặt thì là demo, output ra `var/output/demo`; demo có source .NET nên cần .NET SDK):

```bash
./codemap.sh extract                                # Windows: codemap.cmd extract
./codemap.sh extract --config path/to/config.json   # config khác
```

Các tuỳ chọn khác của `extract` (ví dụ `--at REV`) truyền thẳng vào sau;
`--skip-validate` bỏ bước validate.

Chạy extract lại mỗi khi code đổi. Source nào không đổi sẽ được dùng lại, nên
lần sau nhanh hơn nhiều. Để biết graph còn khớp với code hay không:

```bash
./codemap.sh stale
```

## 4. Bật MCP cho AI agent

Kiểm tra server đọc được output:

```bash
./codemap.sh mcp --self-test
```

Server đọc `output` của config trong `CODE_MAP_CONFIG`; `--config <config>` hoặc
`--graph-output <thư mục output>` để chọn cái khác.

Lệnh in ra graph extract từ commit nào, còn khớp với code không, và danh sách tool.

### GitHub Copilot (VS Code, Agent mode)

Thêm vào `.vscode/mcp.json` của workspace (hoặc chạy lệnh **MCP: Add Server** trong
VS Code), dùng đường dẫn tuyệt đối (bỏ `--config` nếu đã có `CODE_MAP_CONFIG` trong `.env`):

```json
{
  "servers": {
    "codemap": {
      "type": "stdio",
      "command": "/path/to/code-map/.venv/bin/python",
      "args": ["-m", "codemap", "mcp", "--config", "/path/to/code-map.json"]
    }
  }
}
```

Trên Windows: `"command": "C:\\path\\to\\code-map\\.venv\\Scripts\\python.exe"`.
Mở Copilot Chat, chọn **Agent**, rồi bật các tool `codemap_*` trong danh sách tool.

### Claude Code

```bash
claude mcp add codemap -- /path/to/code-map/.venv/bin/python -m codemap mcp --config /path/to/code-map.json
```

### Server dùng chung cho team (HTTP)

Web backend (bước 5) phục vụ luôn MCP tại `http://<host>:8000/code-map/mcp`, đọc
cùng config với web (`CODE_MAP_CONFIG`). Trên PROD đây là cách nên dùng: client
không cần biết đường dẫn trên server.

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
./codemap.sh serve
```

Mở `http://127.0.0.1:8000/code-map/`. Web đọc config trong `CODE_MAP_CONFIG`
(hoặc `--config <config>`); host, port, URL prefix đổi trong mục `web` của config
([docs/REFERENCE.md#cấu-hình-chung](docs/REFERENCE.md#cấu-hình-chung)). Muốn chạy
nền thì dùng `pm2 start web/ecosystem.config.js`.

## Chạy test

```bash
.venv/bin/python -m unittest discover -s extractor/tests -t extractor
.venv/bin/python -m unittest discover -s codemap/tests -t .
.venv/bin/python -m unittest discover -s codemap/mcp/tests -t .
.venv/bin/python -m unittest discover -s web/tests -t .
```
