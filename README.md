# Code Map

Repository gồm hai phần:

- **Engine** `code_tree_exporter/` (trước đây là repo code-tree): scan source
  Angular, .NET API/batch, Oracle PL/SQL, SQL file, XML mapper và ghi graph vào
  `graph.sqlite`.
- **Ứng dụng** `application/` (backend Flask + UI React): import
  `graph.sqlite` vào database phục vụ UI rồi hiển thị flow, lineage, evidence.

```text
source ──extract──▶ output/<run>/graph.sqlite ──import-graph──▶ data/*.sqlite ──▶ API/UI
```

## Chạy demo end-to-end

Yêu cầu: Python 3.10+, .NET SDK 9+ (khuyến nghị; xem phần hiệu năng), Node.js
cho Angular và để build UI.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm install --prefix code_tree_exporter/extractors/angular-extractor typescript
cp .env.example .env        # sửa CODE_MAP_PROJECT_ROOT và CODE_MAP_SOURCE_ROOT
.venv/bin/python scripts/run_demo_pipeline.py --fresh
```

Trên Windows dùng `.venv\Scripts\python.exe`. Script chạy lần lượt `validate`,
`extract` (ra `output/demo/graph.sqlite`), `import-graph` (vào
`data/code-flow-demo.sqlite`) và kiểm tra integrity. Lần đầu có .NET, các worker
được build một lần (vài phút) rồi tái sử dụng.

Chạy UI:

```bash
npm ci --prefix application/frontend && npm run build --prefix application/frontend
.venv/bin/python application/backend/api/server.py   # http://127.0.0.1:8000/code-map/
```

Import một graph khác vào UI:

```bash
.venv/bin/python -m application.backend.cli import-graph <output-dir-or-graph.sqlite>
```

API `POST /api/graph/imports` cũng nhận thư mục output (nằm dưới `output/`) chứa
`graph.sqlite`. Adapter đổi vocabulary của engine sang vocabulary UI
(`READS_FROM`→`READS`/`REMOTE_READS`, `WRITES_TO`→`INSERTS`/`UPDATES`/`DELETES`/
`MERGES`, `HANDLES_API`→`HANDLED_BY`), dùng flow đã materialize của engine để
tạo path đọc/ghi, và lấy tên tiếng Nhật/Anh của bảng, cột từ `input-data/tables*.csv`.

## Hiệu năng: parser SQL trên .NET

Parse Oracle SQL/PL-SQL (ANTLR grammars-v4) là phần nặng nhất. Engine có hai
backend cho cùng grammar, chọn bằng `CODE_TREE_SQL_PARSER`:

| Giá trị | Ý nghĩa |
|---|---|
| `auto` (mặc định) | dùng .NET khi có `dotnet`, nếu không thì Python |
| `dotnet` | bắt buộc .NET, lỗi nếu không chạy được |
| `python` | luôn dùng ANTLR Python (chậm) |

- `code_tree_exporter/extractors/_sql/` (`CodeTree.Sql`): port 1:1 facade
  `oracle_parser.py` và semantic projector sang C#. Offset tính theo code point
  nên kết quả giống hệt bản Python (`tests/test_dotnet_sql_parity.py`).
- `extractors/sql-service/`: service JSON-lines cho extractor Python; extractor
  PL/SQL và SQL file gửi toàn bộ chunk trong một batch, .NET parse song song.
- Extractor Roslyn (.NET API/batch) gọi `CodeTree.Sql` ngay trong process, không
  còn bridge sang Python.
- Worker được build theo major version của SDK đang cài và publish ReadyToRun
  (fallback build thường nếu publish lỗi; tắt bằng `CODE_TREE_DOTNET_READY_TO_RUN=0`).

Demo (không cache): .NET 3.9s, Python 20.6s; graph sinh ra giống hệt nhau.
`CODE_TREE_SQL_THREADS` giới hạn số thread parse của service.

Project chạy theo hai bước độc lập. Bước extract đọc source, tạo node/edge và
ghi vào `graph.sqlite`; bước export Markdown đọc lại database này để tạo
`knowledge/*.md`, `file-trees/*.md`, `codebase-memory/summaries/*.md` và
`SYSTEM_TREE.md`. Vì vậy có thể thay đổi projection hoặc export lại Markdown mà
không phải parse/link source lần nữa.

`graph.sqlite` là nguồn truth; `graph-index.json` và
`codebase-memory/*.jsonl` phục vụ tool/CLI. Comment, encoding và evidence vẫn
được lưu trong SQLite để truy ngược về source.

V3 bổ sung catalog auto-import, hierarchy từ system xuống database/application,
input/output, materialized flow, resolution candidates và quality metrics mà
không đổi stable/numeric ID của graph V2. Config chạy mẫu là
[`demo-config.json`](demo-config.json); bốn CSV bootstrap nằm trong
[`examples/v3/catalog`](examples/v3/catalog).

## V3 bootstrap

Khởi tạo catalog bằng bốn file:

```text
catalog/incoming/database-tables__DB1.csv
catalog/incoming/database-columns__DB1.csv
catalog/incoming/database-tables__DB2.csv
catalog/incoming/database-columns__DB2.csv
```

Thêm vào config:

```json
{
  "catalog": {
    "folder": "${CODE_TREE_CATALOG}",
    "strict": true
  }
}
```

`autoImport=true`, `duplicatePolicy=error`, `encoding=auto` và giới hạn flow đã
có default; chỉ khai báo khi cần override.

Mỗi CSV có structure khác phải có JSON profile trong `catalog/profiles`. Profile
map header nguồn sang schema chuẩn và có thể compile thành `jobnet.csv`,
`executable-mappings.csv` hoặc file legacy khác; importer không tự đoán schema.

## Config

```json
{
  "name": "order-system",
  "root": "${CODE_MAP_SOURCE_ROOT}",
  "output": "${CODE_TREE_OUTPUT}",
  "inputData": "${CODE_MAP_INPUT_DATA}",
  "catalog": {
    "folder": "${CODE_TREE_CATALOG}",
    "strict": true
  },
  "sources": [
    {
      "name": "order-api",
      "type": "dotnet-api",
      "system": "order-system",
      "repository": "order-api",
      "folders": ["api"],
      "database": "DB1",
      "schema": "APP",
      "sqlDialect": "auto"
    }
  ]
}
```

`root`, `output`, `inputData` và `catalog.folder` nhận đường dẫn tuyệt đối hoặc
tương đối với file config. `inputData` chỉ cần cho file legacy chưa chuyển sang
catalog profile. Mọi input phải nằm ngoài `output` và `output.previous`.
`folders` là đường dẫn tương đối; `/` được khuyến nghị. Với `.NET`, `folders`
có thể trỏ tới `.cs`, `.csproj`, `.sln`; pipeline tự stage project/solution
reference closure. XML mapper trong cùng source `.NET` được đọc tự động.
`sqlDialect` của `dotnet-api`/`dotnet-batch` nhận `auto`, `oracle` hoặc `none`.
`auto` chỉ parse embedded SQL khi file có dấu hiệu dùng Oracle; đặt `oracle`
khi source dùng SQL Oracle qua wrapper nội bộ, và `none` để tắt hoàn toàn.

Mặc định `allowPartialExtraction=false`: nếu một extractor lỗi, pipeline không
publish graph mới và output hợp lệ trước đó vẫn được giữ. Chỉ đặt
`allowPartialExtraction=true` khi chấp nhận graph thiếu source và các issue đi
kèm.

Output local nên đặt dưới `.artifacts/code-tree/<tên-lần-chạy>` thay vì tạo các
thư mục `output-*` ở repository root. `.env.example` đã dùng
`.artifacts/code-tree/demo`; toàn bộ `.artifacts/` được Git bỏ qua.

`outputMode` vẫn được giữ để tương thích config cũ. Graph luôn nằm trong một
`graph.sqlite`; logical source partition được lưu bằng `package_key` như
`sources/<source>` hoặc `global`, không tạo lại node/edge ở nhiều file.

Với `oracle-plsql`, `semanticDetail: "summary"` là mặc định: chỉ giữ call, tác
động database và control flow chứa các hành vi đó; các phép gán, `RETURN`, raw
expression và call arguments bị lược bỏ khỏi semantic tree. Dùng
`semanticDetail: "full"` khi cần projection theo từng statement.

`combinedProjection`, `knowledgeChunking` và `maxTreeLines` được lưu vào
metadata SQLite làm mặc định cho bước export Markdown. `maxTreeLines` chỉ giới
hạn projection Markdown; `maxFileBytes` ghi
`FILE_TOO_LARGE` và bỏ riêng file đó. `extractorTimeoutSeconds` (mặc định 3600) giới hạn
toàn bộ worker; `projectTimeoutSeconds` (mặc định 900) giới hạn từng lần Roslyn
mở solution/project. Config bị từ chối nếu `extractorTimeoutSeconds` nhỏ hơn
`projectTimeoutSeconds`, vì khi đó worker bị kill trước khi kịp load xong một
project lớn và source biến mất khỏi graph.

Runtime tùy extractor: Python 3.10+, Node.js + TypeScript cho Angular, .NET SDK
9+ cho Roslyn. Có thể chỉ định executable không nằm trong `PATH` bằng
`CODE_TREE_NODE` và `CODE_TREE_DOTNET`. Runner tự suy ra `DOTNET_ROOT`, host
path, SDK resolver và runtime roll-forward từ executable đã chọn. Thiếu
Node.js/TypeScript hoặc primary parser lỗi, Angular dùng Python fallback, đồng
thời ghi `SEMANTIC_TREE_UNAVAILABLE`; fallback không bảo toàn đầy đủ nested
behavior.

Sao chép `.env.example` thành `.env` trên mỗi máy rồi sửa path. CLI tự đọc `.env` cạnh file config; nếu không có thì đọc `.env` tại thư mục chạy. Biến đã export trong process có ưu tiên cao hơn `.env`. `.env` bị Git bỏ qua; chỉ `.env.example` được commit để liệt kê cấu hình cần thiết.

`defaultEncoding: "auto"` nhận diện theo thứ tự: BOM, khai báo encoding ở header
Python/XML/HTML hoặc comment header, UTF-8 strict, UTF-16 heuristic, rồi
CP932/EUC-JP strict. Kết quả legacy mơ hồ tạo `ENCODING_CONFLICT`; không dùng ký
tự replacement nên không âm thầm làm hỏng tiếng Nhật. `encoding` và
`encodingOverrides` vẫn dùng để khóa encoding cho source/file đặc biệt.

## Cài đặt

- macOS: `python3 -m pip install -r requirements.txt`
- Windows: `py -m pip install -r requirements.txt`
- Cài CLI từ source: `python3 -m pip install .` hoặc `py -m pip install .`

Node.js + TypeScript và .NET SDK 9 là runtime ngoài Python; chỉ cần cài khi
config dùng Angular hoặc .NET. Extractor `.NET` dùng `MSBuildWorkspace`, nên
SDK mà `global.json`/project yêu cầu cũng phải có trên máy. Nếu workspace không
load được project, extractor ghi `MSBUILD_WORKSPACE_DIAGNOSTIC` và chỉ dùng
fallback compilation cho các file bị ảnh hưởng. Lần chạy đầu build worker
Release; các lần sau chạy DLL trực tiếp nếu source worker không đổi. Runner tự
cấu hình môi trường .NET từ `CODE_TREE_DOTNET` hoặc executable trong `PATH`. Nếu
TypeScript semantic runtime không load được, Angular dùng fallback chỉ giữ
declaration/literal với confidence tối đa `0.5` và ghi
`SEMANTIC_TREE_UNAVAILABLE`.

## Chạy

### Demo

Repository có sẵn `sample-source/`, `input-data/` và catalog mẫu. Chỉ chạy engine
(không import vào UI):

```text
cp .env.example .env
python3 -m code_tree_exporter validate --config "$PWD/demo-config.json"
python3 -m code_tree_exporter extract --config "$PWD/demo-config.json"
```

`validate` kiểm tra cả runtime ngoài (`dotnet` cho source `.NET`, `node` cho
Angular) và trả exit code `1` khi thiếu runtime bắt buộc. `extract` cũng dừng
ngay từ đầu trong trường hợp này thay vì chạy các extractor khác rồi mới báo
lỗi. Khi có extractor lỗi, thông báo liệt kê mọi source lỗi kèm lý do.

### Cache DFA cho backend Python

Khi dùng backend Python, parser ANTLR PL/SQL mất 5–20 giây cho lần parse đầu tiên trong
mỗi process, vì phải dựng DFA dự đoán. Sau lần chạy đầu, DFA được lưu vào cache
cho từng entry point (`plsql-extractor`, `sql-file-extractor`, bridge SQL của
.NET, pipeline) nên các lần sau chỉ cần nạp lại. Với demo, thời gian giảm từ
khoảng 21s xuống khoảng 6.5s, output không đổi.

- `CODE_TREE_CACHE_DIR`: đổi thư mục cache (mặc định `~/.cache/code-tree`, hoặc
  `%LOCALAPPDATA%\code-tree\cache` trên Windows).
- `CODE_TREE_ANTLR_CACHE=0`: tắt cache.

Cache tự bị bỏ qua khi grammar, phiên bản ANTLR runtime hoặc Python thay đổi.
Mỗi file cache khoảng 20–30 MB; có thể xóa bất cứ lúc nào.

### 1. Extract graph vào SQLite

```text
code-tree-exporter validate --config <absolute-config-path>
code-tree-exporter extract --config <absolute-config-path>
```

`code-tree-exporter --config <absolute-config-path>` vẫn được giữ để tương thích V2.
Kiểm tra một CSV lạ bằng `code-tree-exporter catalog inspect <csv-path>` trước
khi viết profile.

Chạy trực tiếp từ source:

- macOS: `python3 -m code_tree_exporter --config /path/extractor-config.json`
- Windows: `py -m code_tree_exporter --config C:\\path\\extractor-config.json`

`python -m cli` vẫn được giữ làm lệnh tương thích.

Bước này tạo `graph.sqlite` và các index/memory dạng machine-readable, không tạo
file Markdown.

### 2. Export Markdown từ SQLite

Ghi Markdown cạnh database:

```text
code-tree-export-markdown --database <dist/graph.sqlite>
```

Hoặc ghi sang thư mục riêng:

```text
code-tree-export-markdown --database <dist> --output <markdown-dist>
```

`--database` nhận cả file `graph.sqlite` lẫn thư mục chứa file đó. Có thể override
config đã lưu bằng `--max-tree-lines`, `--combined-projection` hoặc
`--no-combined-projection`.

Chạy trực tiếp từ source:

```text
python3 -m code_tree_exporter.markdown_export --database <dist>
```

## Query knowledge

Sau khi generate output, query layer đọc manifest và query trực tiếp
`graph.sqlite` qua các index SQLite. `graph-index.json` chỉ được nạp lazy khi
cần locator của codebase memory:

```text
code-tree-query --output <dist> find-node --qualified-name <name>
code-tree-query --output <dist> find-edges --source <node-id>
code-tree-query --output <dist> impact-api --method GET --path /orders/42
code-tree-query --output <dist> impact-table --database DB --schema APP --table ORDERS
code-tree-query --output <dist> trace-ui-to-db --query "GET /orders/{id}"
code-tree-query --output <dist> explain-node --node-id <node-id>
code-tree-query --output <dist> open-source --evidence-id <evidence-id>
code-tree-query --output <dist> list-issues --source order-api
code-tree-query --output <dist> search-memory --text orders
code-tree-query --output <dist> catalog-status
code-tree-query --output <dist> health
code-tree-query --output <dist> unresolved
code-tree-query --output <dist> input-output --node-id <node-id>
code-tree-query --output <dist> trace-flow --node-id <node-id>
```

Chạy trực tiếp từ source bằng
`python -m code_tree_exporter.query --output <dist> ...`. Response là JSON ngắn
gồm answer, graph IDs, evidence, source location, confidence và issues. Trace
được chặn ở 5.000 node, 10.000 edge và 5.000 evidence; kiểm tra
`data.truncated` và dùng `graph.sqlite` khi cần duyệt toàn bộ graph rất lớn.

## Cấu trúc source

```text
code_tree_exporter/     # engine: package, contract, extractor runtimes
  extractors/_sql/      # CodeTree.Sql: ANTLR PL/SQL trên .NET
  extractors/sql-service/  # service JSON-lines cho extractor Python
application/            # backend API (Flask) + frontend (React)
  backend/importer/graph_sqlite.py  # import graph.sqlite vào DB phục vụ UI
sample-source/          # source demo
input-data/             # CSV thủ công (jobnet, tên JA/EN của bảng, ...)
examples/v3/catalog/    # catalog demo cho engine
scripts/                # run_demo_pipeline.py
cli.py                  # compatibility shim
pyproject.toml          # build, dependency, package resources
```

## Output sau bước extract

```text
dist/
├── manifest.json
├── graph-index.json
├── graph.sqlite
├── quality-report.json
├── QUALITY_REPORT.md
├── codebase-memory/
│   ├── entities/*.jsonl
│   ├── relationships/*.jsonl
│   └── manifest.json
```

## Output sau bước export Markdown

Nếu không truyền `--output`, các file sau được thêm vào `dist/`:

```text
dist/
├── markdown-manifest.json
├── codebase-memory/
│   └── summaries/*.md
├── knowledge/
│   ├── manifest.json
│   └── APIs*.md, Flows*.md, Databases*.md, Jobs*.md, CrossSystem*.md
├── file-trees/
└── SYSTEM_TREE.md (khi combinedProjection=true)
```

Trong SQLite, `node_id`/`edge_id` là số nguyên 63-bit deterministic. ID mô tả
dài trước đây chỉ được lưu một lần ở cột `stable_id`; query CLI chấp nhận cả ID
số mới và stable ID cũ. Edge/evidence/comment/issue tham chiếu bằng khóa số nên
không lặp lại chuỗi ID dài. JSON response biểu diễn ID số dưới dạng decimal
string để không mất precision trên JavaScript.

Output extract được dựng trong staging rồi thay atomic. Rerun giữ snapshot trước
tại `<output>.previous`. Markdown exporter chỉ thay các projection do tool quản
lý và từ chối ghi đè output không có manifest hợp lệ. Decode strict; lỗi
encoding tạo issue, file lỗi không được parse.
