# Code Map

Repository gồm ba phần (code-tree đã gộp vào đây):

- **`extractor/`**: package `code_tree_exporter`. Scan source Angular, .NET
  API/batch, Oracle PL/SQL, SQL file, XML mapper và ghi ra `graph.sqlite` cùng
  `codemap-rag.sqlite`.
- **`web/`**: backend Flask và UI React. Đọc thẳng output của extract và hiển
  thị flow, lineage, evidence.
- **`vscode-extension/`**: `@codemap` trong VS Code Chat (Graph RAG) và map
  luồng nghiệp vụ vẽ ngay trong VS Code.

Thư mục dùng chung: `shared/` (fixtures và hợp đồng dữ liệu giữa các phần),
`examples/` (source demo, input-data, catalog, `demo-config.json`) và
`scripts/`.

```text
source ──extract──▶ output/<run>/ ─┬─ graph.sqlite         (graph đầy đủ của engine)
                                   ├─ codemap-web.sqlite   ──▶ web (đọc trực tiếp)
                                   └─ codemap-rag.sqlite   ──▶ VS Code @codemap
```

Không còn bước import. Web mở thẳng thư mục output, ghi chú và gán thuật ngữ do
người dùng thêm nằm riêng trong `data/knowledge.sqlite` nên lần extract sau
không xoá mất.

## Chạy demo end-to-end

Yêu cầu: Python 3.10+, .NET SDK 9+ (khuyến nghị; xem phần hiệu năng), Node.js
cho Angular và để build UI.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm ci --prefix extractor/code_tree_exporter/extractors/angular-extractor   # TypeScript 5.9 (7.x has no JS API)
cp .env.example .env        # sửa CODE_MAP_PROJECT_ROOT, CODE_MAP_SOURCE_ROOT, CODE_MAP_GRAPH_OUTPUT
.venv/bin/python scripts/run_demo_pipeline.py
```

Trên Windows dùng `.venv\Scripts\python.exe`. Script chạy `validate` rồi
`extract`, ra `output/demo/`. Lần đầu có .NET, các worker được build một lần
(vài phút) rồi dùng lại.

Chạy UI:

```bash
npm ci --prefix web/frontend && npm run build --prefix web/frontend
.venv/bin/python web/backend/api/server.py   # http://127.0.0.1:8000/code-map/
```

Web đọc thư mục trong `CODE_MAP_GRAPH_OUTPUT` (file `.env`). Muốn xem một lần
extract khác thì đổi biến này, hoặc chạy bản dev
`python web/backend/api/app.py --graph-output <thư mục output>`.

`codemap-web.sqlite` đã chuyển sẵn vocabulary của engine sang vocabulary của UI:
- `READS_FROM` → `READS`/`REMOTE_READS`
- `WRITES_TO` → `INSERTS`/`UPDATES`/`DELETES`/`MERGES`
- `HANDLES_API` → `HANDLED_BY`

File này cũng chứa:
- path đọc/ghi lấy từ flow của engine;
- tên tiếng Nhật/Anh/Việt của bảng và cột lấy từ `inputData`;
- từ điển nghiệp vụ;
- job batch lấy từ `jobnet.csv`.

`semantic_tree` (có thể nặng vài MB mỗi node) không chép sang file này. Web đọc
nó từ `graph.sqlite` khi mở chi tiết một node. Nếu thư mục output được sinh bởi
bản extract cũ, API trả lỗi `503 graph_not_ready` kèm hướng dẫn chạy lại.

## Hiệu năng: parser SQL trên .NET

Parse Oracle SQL/PL-SQL (ANTLR grammars-v4) là phần nặng nhất. Engine có hai
backend cho cùng grammar, chọn bằng `CODE_TREE_SQL_PARSER`:

| Giá trị | Ý nghĩa |
|---|---|
| `auto` (mặc định) | dùng .NET khi có `dotnet`, nếu không thì Python |
| `dotnet` | bắt buộc .NET, lỗi nếu không chạy được |
| `python` | luôn dùng ANTLR Python (chậm) |

- `extractor/code_tree_exporter/extractors/_sql/` (`CodeTree.Sql`): port 1:1 facade
  `oracle_parser.py` và semantic projector sang C#. Offset tính theo code point
  nên kết quả giống hệt bản Python (`extractor/tests/test_dotnet_sql_parity.py`).
- `extractors/sql-service/`: service JSON-lines cho extractor Python; extractor
  PL/SQL và SQL file gửi toàn bộ chunk trong một batch, .NET parse song song.
- Extractor Roslyn (.NET API/batch) gọi `CodeTree.Sql` ngay trong process, không
  còn bridge sang Python.
- Worker được build theo major version của SDK đang cài và publish ReadyToRun
  (fallback build thường nếu publish lỗi; tắt bằng `CODE_TREE_DOTNET_READY_TO_RUN=0`).

Demo (không cache): .NET 3.9s, Python 20.6s; graph sinh ra giống hệt nhau.
`CODE_TREE_SQL_THREADS` giới hạn số thread parse của service (mặc định nửa số core).

## Repo lớn: giữ máy không bị lag

Đo trên repo giả lập 4.410 file / 510.000 dòng (85k node, 190k edge):

| | Trước | Sau |
|---|---|---|
| Extract | 488s, RAM process chính 2,2GB | 158s, 1,0GB |
| `graph-index.json` | 149MB | 5MB |
| Import vào DB của UI | > 10 phút | không còn bước này (web đọc thẳng output) |
| Click node hub trong UI (`/detail`) | 365ms, 1,9MB | 5ms, 5KB |
| Search xấu nhất | 954ms | 93ms |

Mặc định đã chọn để máy vẫn dùng được trong lúc scan:

- Source được đọc tại chỗ, không copy. File không phải UTF-8 (Shift_JIS, CP932,
  EUC-JP, UTF-16, UTF-8 có BOM) được ghi vào `sourceEncodings` trong config của
  từng extractor, rồi Python, Roslyn (cả document của MSBuild) và TypeScript tự
  giải mã các file đó. Trước đây, chỉ cần một file như vậy là cả source bị copy
  sang thư mục tạm.
- `lowPriority` (mặc định `true`): pipeline tự hạ priority (`nice` trên
  macOS/Linux, `BELOW_NORMAL` trên Windows); extractor, Roslyn worker và SQL
  service kế thừa. Đặt `"lowPriority": false` để chạy hết tốc lực.
- `limits.maxParallelExtractors` mặc định là nửa số core (tối đa 4). Máy ít RAM
  hoặc solution .NET rất lớn: đặt `1` để các extractor chạy lần lượt, peak RAM
  bằng extractor lớn nhất thay vì tổng của chúng.
- `graphIndex` mặc định `"slim"`: `graph-index.json` chỉ còn số đếm, khóa API/
  bảng và locator của codebase memory; mọi tra cứu theo id đi qua
  `graph.sqlite`. Dùng `"graphIndex": "full"` nếu tool ngoài cần bản cũ.
- Worker .NET chạy với `DOTNET_GCConserveMemory=7`; SQL service dùng workstation GC.
- UI mở rộng tối đa 200 node mỗi lần (chỉnh được tới 1000); `/detail` mà UI gọi
  dùng `relations=0` (chỉ số đếm), danh sách quan hệ lấy qua `/flow`.

Phần tốn RAM nhất còn lại là Roslyn worker (khoảng 1,6GB cho 1.500 file C#), vì
nó giữ compilation và semantic model của cả solution. Với solution rất lớn, tách
thành nhiều source theo nhóm project trong config để mỗi worker nhỏ hơn.

Project chạy theo hai bước độc lập. Bước extract đọc source, tạo node/edge và
ghi vào `graph.sqlite`; bước export Markdown đọc lại database này để tạo
`knowledge/*.md`, `file-trees/*.md`, `codebase-memory/summaries/*.md` và
`SYSTEM_TREE.md`. Vì vậy có thể thay đổi projection hoặc export lại Markdown mà
không phải parse/link source lần nữa.

`graph.sqlite` là nguồn truth; `graph-index.json` (mặc định bản slim) và
`codebase-memory/*.jsonl` phục vụ tool/CLI. Comment, encoding và evidence vẫn
được lưu trong SQLite để truy ngược về source.

V3 bổ sung catalog auto-import, hierarchy từ system xuống database/application,
input/output, materialized flow, resolution candidates và quality metrics mà
không đổi stable/numeric ID của graph V2. Config chạy mẫu là
[`examples/demo-config.json`](examples/demo-config.json); bốn CSV bootstrap nằm trong
[`examples/catalog`](examples/catalog).

## Phiên bản, độ tươi và extract lại nhanh

Mỗi lần extract ghi commit của từng source (`git rev-parse HEAD`, branch, có
sửa chưa commit hay không) vào `manifest.json` (`revisions`), metadata
`source_revisions` của `graph.sqlite` và bảng `meta` của `codemap-rag.sqlite`.
Bảng `sources` đã có sẵn sha256 của từng file.

- **Graph còn đúng với code không:**
  `code-tree-exporter stale --graph output/demo` liệt kê file đã sửa, đã xoá,
  file mới, số commit mới của từng source và các node khai báo trong file đã
  sửa. Exit code `0` khi graph còn khớp với working tree, `1` khi đã cũ.
  MCP server dùng kết quả này để mở đầu mọi câu trả lời bằng một dòng tình
  trạng KB.
- **Extract lại nhanh (incremental):** source nào có code, config, input data,
  phiên bản extractor và runtime (TypeScript, .NET SDK) không đổi thì dùng lại
  package của lần trước thay vì parse lại. Cache nằm ở `<output>.cache/` (đổi
  bằng `cacheDir`), giữ 3 bản gần nhất cho mỗi source. Tắt bằng
  `"incremental": false` hoặc `CODE_TREE_INCREMENTAL=0`.
- **Extract một commit cũ:** `extract --config <cfg> --at <rev> --output <thư mục>`
  checkout bằng `git worktree` vào thư mục tạm (working tree không bị đụng tới).
  Có thể chỉ định theo từng source `--at order-api=v1.2 --at order-db=abc123`,
  hoặc theo ngày `--at-date 2026-01-31` (commit cuối trước ngày đó của mỗi repo).
  Dùng cho graph diff của PR (base và head) và cho backtest.

`shared/fixtures/golden/` chứa snapshot graph của `examples/sample-source`
(`extractor/tests/test_golden_demo.py`). Đổi parser làm đổi graph thì test báo
từng node/cạnh khác đi; nếu thay đổi là chủ ý, chạy lại test với
`CODE_MAP_UPDATE_GOLDEN=1` rồi review diff trong commit. CI
(`.github/workflows/ci.yml`) chạy test extractor, web, extension và frontend.

## MCP server cho AI agent (Copilot, Claude Code, Cursor…)

Kênh chính để AI agent dùng Code Map là MCP. Server đọc thư mục output của
extract (`codemap-rag.sqlite`, `codemap-web.sqlite`, `graph.sqlite`) và
`knowledge.sqlite` (tài liệu nghiệp vụ đã duyệt). Có hai cách chạy, dùng chung
một code (`web/backend/codemap_mcp/`):

- **stdio** (máy cá nhân):
  `python -m web.backend.codemap_mcp --graph-output output/demo [--knowledge-db data/knowledge.sqlite]`
- **HTTP** (server chung của team): web backend phục vụ luôn `POST <url_prefix>/mcp`,
  ví dụ `http://host:8000/code-map/mcp`. Tên người gọi lấy từ header
  `X-Code-Map-User`. Tool ghi (đề xuất vào hàng chờ duyệt) chỉ hiện khi gửi
  `X-Code-Map-Token` bằng `CODE_MAP_KNOWLEDGE_TOKEN`. Request từ trình duyệt
  có `Origin` lạ bị từ chối (thêm vào `CODE_MAP_MCP_ALLOWED_ORIGINS` nếu cần).

Cấu hình cho VS Code nằm ở [`.vscode/mcp.json`](.vscode/mcp.json).

Mỗi tool ứng với một bước của quy trình AIDD:

| Tool | Dùng khi |
|---|---|
| `codemap_search` | Hỏi tự do (vi/ja/en hoặc tên kỹ thuật) trước khi đọc code |
| `codemap_current_behavior` | Hiện trạng một chủ đề nghiệp vụ: code, quy tắc, tài liệu luồng, câu đã trả lời, kèm trạng thái duyệt |
| `codemap_impact` | Trước khi sửa bảng/API/procedure/job, hoặc truyền `files` đang sửa; nhóm theo system |
| `codemap_trace_flow` | Đi theo một màn hình/API/job qua mọi system tới dữ liệu |
| `codemap_file_context` | Một file khai báo gì, chạm tới gì |
| `codemap_check_terms` | Kiểm tra requirement: thuật ngữ, tên kỹ thuật không tồn tại, từ mơ hồ |
| `codemap_status` | Graph extract từ commit nào, file nào đã cũ, chất lượng extract |

Mọi câu trả lời mở đầu bằng một dòng tình trạng KB (commit, đã cũ hay chưa),
trích dẫn bằng `[N…]`/`[E…]`/`[D…]` và kết thúc bằng mục **Not certain**
(issue của extractor, file đã sửa sau extract, giới hạn của phân tích tĩnh).
Server cũng có MCP prompt cho từng bước (`as-is`, `decision-table`, `impact`,
`review-requirement`, `document-flow`, `review-change`), hiện thành lệnh `/`
trong Copilot. Mọi lần gọi tool được ghi vào bảng `mcp_calls` của
`knowledge.sqlite`, kể cả câu hỏi không ra kết quả, để biết KB đang thiếu gì.

Retriever của server là bản Python của `vscode-extension/src/core/retriever.ts`;
`vscode-extension/test/retriever.test.ts` kiểm tra hai bản cho ra cùng text.

## Graph RAG trong VS Code (extension)

Từ khi Copilot dùng được MCP, extension chỉ còn cần cho map panel; phần chat
`@codemap` được giữ để tương thích nhưng không phát triển thêm.


Mỗi lần extract đều ghi thêm `codemap-rag.sqlite` cạnh `graph.sqlite`. File này
gồm graph đã lược gọn và dữ liệu nghiệp vụ trong `inputData`: từ điển nghiệp vụ,
tên bảng/cột ja/en, jobnet. Kèm theo là chỉ mục từ khoá. Muốn tắt thì đặt
`"ragExport": false` trong config. Nếu đã có output thì tạo lại bằng:

```bash
PYTHONPATH=extractor python -m code_tree_exporter rag-export --graph output/demo --input-data examples/input-data
```

Extension `vscode-extension/` đọc file này và trả lời bằng model chat của VS Code:
`@codemap hủy đơn hàng ghi vào bảng nào?`, `/impact`, `/flow`, `/explain`,
`/gaps`. Ngoài ra có tool `#codemap` cho Agent mode. Cách build và cài xem
[vscode-extension/README.md](vscode-extension/README.md).

Đo trên nopCommerce (17.585 node, 83.485 cạnh):
- Xuất file: 14 giây, file 47MB (graph gốc 223MB).
- Mỗi câu hỏi: 13–72 ms.
- RAM của extension: 47–107MB.

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
Node.js/TypeScript hoặc primary parser lỗi thì source Angular **báo lỗi và
extract dừng, giữ nguyên output cũ**: Python fallback đặt id node khác (không có
UI action, id service/component khác) nên dùng nó âm thầm sẽ làm rời mọi tài
liệu nghiệp vụ. Muốn chấp nhận fallback, đặt `CODE_TREE_ANGULAR_FALLBACK=1`
(ghi `SEMANTIC_TREE_UNAVAILABLE`).

Sao chép `.env.example` thành `.env` trên mỗi máy rồi sửa path. CLI tự đọc `.env` cạnh file config; nếu không có thì đọc `.env` tại thư mục chạy. Biến đã export trong process có ưu tiên cao hơn `.env`. `.env` bị Git bỏ qua; chỉ `.env.example` được commit để liệt kê cấu hình cần thiết.

`defaultEncoding: "auto"` nhận diện theo thứ tự: BOM, khai báo encoding ở header
Python/XML/HTML hoặc comment header, UTF-8 strict, UTF-16 heuristic, rồi
CP932/EUC-JP strict. Kết quả legacy mơ hồ tạo `ENCODING_CONFLICT`; không dùng ký
tự replacement nên không âm thầm làm hỏng tiếng Nhật. `encoding` và
`encodingOverrides` vẫn dùng để khóa encoding cho source/file đặc biệt.

## Chạy trên Windows (source tiếng Nhật)

1. **Cài:** Python 3.11+ (bản từ python.org; tick "Add to PATH"), .NET SDK 9+
   (cùng major với `global.json` của solution cần scan), Node.js 20+ (bản
   chính thức có full ICU, cần để giải mã Shift_JIS trong Angular), Git.
2. **Bật đường dẫn dài** (repo sâu, `node_modules`), PowerShell quyền Admin:
   ```powershell
   New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
   git config --global core.longpaths true
   ```
3. **Virtualenv và thư viện:**
   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   npm ci --prefix extractor\code_tree_exporter\extractors\angular-extractor
   ```
4. **UTF-8 cho Python** (khuyến nghị): biến môi trường hệ thống
   `PYTHONUTF8=1`. Engine đã tự đặt cho các tiến trình con, biến này giúp cả
   các script bạn tự viết đọc ghi UTF-8 thay vì cp932.
5. **`.env`:** đường dẫn tuyệt đối viết bình thường kiểu Windows, không cần
   escape:
   ```
   CODE_MAP_PROJECT_ROOT=C:\work\code-map
   CODE_MAP_SOURCE_ROOT=C:\work\code-map\examples\sample-source
   CODE_MAP_GRAPH_OUTPUT=C:\work\code-map\output\demo
   CODE_TREE_OUTPUT=../output/demo
   CODE_MAP_INPUT_DATA=./input-data
   CODE_TREE_CATALOG=./catalog
   ```
   Không ghi dấu `;` cuối dòng, không dùng `...`. Giá trị bắt đầu bằng `./`
   hoặc `../` được tính từ file config JSON.
6. **Chạy:** `.venv\Scripts\python scripts\run_demo_pipeline.py`, web chạy bằng
   `.venv\Scripts\python web\backend\api\server.py` hoặc
   `pm2 start web\ecosystem.config.js`. pm2 tự chọn `Scripts\python.exe`.

**Encoding của source:**
- Mặc định `defaultEncoding: "auto"`: mỗi file được nhận diện riêng: BOM,
  khai báo trong file (`<?xml encoding=…?>`, `-- coding:`), rồi đoán giữa
  UTF-8, CP932, EUC-JP. Source Shift_JIS trên Windows gần như luôn được nhận
  là **CP932**. CP932 là tập lớn hơn Shift_JIS chuẩn, có cả ký tự NEC/IBM như
  `①`, `㈱`, `髙`.
- Nếu một thư mục chắc chắn dùng một encoding, khoá lại cho chắc và nhanh hơn:
  ```json
  { "name": "order-db", "type": "oracle-plsql", "folders": ["order-db"],
    "encoding": "cp932",
    "encodingOverrides": [ { "glob": "order-db/legacy/*", "encoding": "euc_jp" } ] }
  ```
- File không giải mã được thì bị bỏ qua và có issue `ENCODING_ERROR` (kèm byte
  lỗi) trong `QUALITY_REPORT.md`, không bao giờ bị thay ký tự âm thầm.
- CSV trong `inputData` (`tables.csv`, `semantic-dictionary.csv`, `jobnet.csv`…)
  lưu từ Excel dạng "CSV" (CP932) hay "CSV UTF-8" đều đọc được.
- Không cần bật "Beta: Use Unicode UTF-8 for worldwide language support" của
  Windows; bật cũng không sao.

**Lưu ý khác trên Windows:**
- Git: nên đặt `git config core.autocrlf false` cho repo source cần scan, để
  hash và evidence giống nhau giữa các máy. CRLF không làm sai số dòng.
- Khi extract xong, output được thay bằng thao tác đổi tên thư mục. Nếu web
  đang mở file đúng lúc đó, engine thử lại trong khoảng 10 giây.
- Antivirus (Defender) quét `node_modules` và `.msbuild` làm chậm. Có thể
  thêm thư mục repo và thư mục output vào danh sách loại trừ.

## Cài đặt

- macOS: `python3 -m pip install -r requirements.txt`
- Windows: `py -m pip install -r requirements.txt`
- Cài CLI `code-tree-exporter` từ source: `python3 -m pip install ./extractor`
  (hoặc `py -m pip install ./extractor`). Nếu không cài, chạy
  `python -m code_tree_exporter` từ thư mục `extractor/` hoặc đặt
  `PYTHONPATH=extractor`.

Node.js + TypeScript và .NET SDK 9 là runtime ngoài Python; chỉ cần cài khi
config dùng Angular hoặc .NET. Extractor `.NET` dùng `MSBuildWorkspace`, nên
SDK mà `global.json`/project yêu cầu cũng phải có trên máy. Nếu workspace không
load được project, extractor ghi `MSBUILD_WORKSPACE_DIAGNOSTIC` và chỉ dùng
fallback compilation cho các file bị ảnh hưởng. Lần chạy đầu build worker
Release; các lần sau chạy DLL trực tiếp nếu source worker không đổi. Runner tự
cấu hình môi trường .NET từ `CODE_TREE_DOTNET` hoặc executable trong `PATH`. Nếu
TypeScript semantic runtime không load được, Angular chỉ dùng fallback khi
`CODE_TREE_ANGULAR_FALLBACK=1` (giữ declaration/literal với confidence tối đa
`0.5` và ghi `SEMANTIC_TREE_UNAVAILABLE`).

## Chạy

### Demo

Repository có sẵn `examples/sample-source/`, `examples/input-data/` và catalog
mẫu. Chỉ chạy engine (không import vào UI):

```text
cp .env.example .env
cd extractor
python3 -m code_tree_exporter validate --config "$PWD/../examples/demo-config.json"
python3 -m code_tree_exporter extract --config "$PWD/../examples/demo-config.json"
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

## Tài liệu nghiệp vụ (menu → tính năng → luồng)

Màn hình chính của web là tài liệu nghiệp vụ gắn với graph. Graph explorer cũ đã
gộp vào đây thành **trang node** (`#/node/<node_id>`): tìm một method, API, bảng…
ở ô tìm kiếm, hoặc bấm "Chi tiết và graph từ node này" trên một node trong luồng.
Trang node có graph với checkbox 1/W/R/E, panel chi tiết (nghĩa nghiệp vụ, cột,
semantic tree) và danh sách luồng có chứa node.

**Cây tính năng dựng tự động từ graph** sau mỗi lần extract:

- mỗi `SCREEN` (route Angular) là một tính năng, gom vào menu theo đoạn đầu
  của route (`orders/new`, `orders/:id` → menu `orders`);
- mỗi `JOB_NETWORK` (jobnet.csv) là một tính năng trong menu "Chạy nền";
- mỗi tính năng đề xuất một luồng cho từng `UI_ACTION` (màn hình) hoặc `JOB`
  (jobnet). Các bước của luồng được tách từ graph theo hệ thống mà luồng đi qua
  (customer-web → order-api → ORDER_DB …), nên swimlane có một lane cho mỗi hệ
  thống và mỗi bước nằm trong lane của nó;
- `API_OPERATION` không tính năng nào gọi tới nằm ở "Chưa xếp vào tính năng",
  có thể gán vào một tính năng.

**Người dùng không thêm bước bằng tay.** AI viết nội dung, người dùng đọc,
sửa và duyệt. Tên menu/tính năng, mô tả, tài liệu luồng, trạng thái duyệt và
câu trả lời lưu trong `knowledge.sqlite` (bảng `biz_items`, `biz_flows`,
`biz_exports`), gắn theo id của graph nên không mất khi extract lại. Tài liệu
nào không còn tính năng sau khi extract lại (màn hình/UI action bị xoá hoặc đổi
route) hiện ở menu **Tài liệu không còn tính năng**, không bị mất. Nên sao lưu
`knowledge.sqlite` trước mỗi lần extract cho bản release.

**Viết bằng AI** (ở trang tính năng hoặc đầu tài liệu luồng):

1. **Xuất cho AI** tạo một file hướng dẫn `.md` cho tính năng gồm: phần hướng
   dẫn (dữ liệu có gì, cần điền gì, quy tắc trích dẫn và bằng chứng, ví dụ),
   phần **Dữ liệu** (khối YAML `context`: node, cạnh, đoạn code, comment, điều
   kiện IF/EXCEPTION lấy từ `semantic_tree`, câu trả lời đã có) và phần **Định
   dạng trả về** (khối YAML `export` + `flows` cần điền; phần đã sửa tay hoặc đã
   duyệt có `locked: true`). Bỏ tick "Kèm hướng dẫn cho AI" để chỉ lấy file YAML.
2. Đính kèm file vào AI; không cần câu lệnh riêng. AI trả về đúng khối YAML
   `export` + `flows` (có thể nằm trong khối ```` ```yaml ````).
3. **Nhập kết quả** kiểm tra file thuộc đúng lần xuất của tính năng, graph đã
   đổi chưa, mọi tham chiếu `[[tên]]` / `loại: tên` có trong graph không, quy
   tắc nào thiếu evidence; rồi ghi nội dung mới ở trạng thái "AI viết · chưa
   duyệt". Phần khoá không bị ghi đè kể cả khi AI sửa vào.

Tham chiếu node trong tài liệu dùng tên: `Class.method` cho code, tên kỹ thuật
cho phần còn lại (`POST /api/orders`, `ORDER_HEADER`). Trong YAML có thêm loại:
`api: POST /api/orders`, `method: OrderController.Create`.

**Quy tắc, quyết định và câu hỏi** (kiến thức chỉ con người nói được, lưu trong
`knowledge.sqlite`, gắn theo id của graph):

- `RULE-<DOMAIN>-001`: quy tắc nghiệp vụ kèm evidence (file:dòng), các node hiện
  thực nó, requirement và test liên quan. Trạng thái `draft` → `approved` →
  (`needs_review`) → `retired`. AI chỉ tạo được `draft`.
- `DEC-0001`: quyết định kiểu ADR (bối cảnh, các phương án, quyết định, lý do).
- `Q-0001`: câu hỏi cho người, có phương án; trả lời xong thì AI dùng câu trả lời
  như sự thật và không hỏi lại.
- **Tự đưa về review khi code đổi:** mỗi lần extract ghi fingerprint hành vi của
  từng node (semantic tree + cạnh đi ra, bỏ số dòng: sửa format, comment hay chèn
  code phía trên không làm đổi). Quy tắc đã duyệt và phần tài liệu luồng đã duyệt
  lưu fingerprint lúc duyệt; extract sau mà node đổi hành vi hoặc bị xoá thì quy
  tắc chuyển `needs_review` kèm lý do, phần tài liệu vào hàng chờ duyệt. MCP
  server ghi rõ "NEEDS REVIEW" và đưa vào mục Not certain. Chạy kiểm tra ngay sau
  extract: `python -m web.backend.kb revalidate --graph-output <out>` (exit `1`
  khi có quy tắc bị đưa về review).
- `inputData/owners.csv` (`system,team,owner,contact,notes`) cho biết ai nắm
  system nào; `codemap_impact` ghi owner cạnh từng system.
  `inputData/ops-facts.csv` (`target,kind,value,note`: lịch batch, khối lượng
  dữ liệu, SLA, lock) hiện trong câu trả lời về node đó.

API (dưới `CODE_MAP_GRAPH_API_PREFIX`, mặc định `/api/graph`):

| Route | Ý nghĩa |
|---|---|
| `GET /business/tree` | cây menu → tính năng → luồng |
| `GET/PATCH /business/items/<id>` | chi tiết, đổi tên/mô tả/duyệt, gán entry point |
| `GET /business/flows/<id>` | tài liệu luồng kèm node và cạnh |
| `PATCH /business/flows/<id>/parts/<summary\|edge_cases\|step:sN>` | sửa hoặc duyệt một phần |
| `POST /business/flows/<id>/questions/<qid>/answer` | trả lời câu hỏi |
| `POST /business/features/<id>/ai-export` | tạo file cho AI (`?code=0&locked=0&answers=0` để bỏ bớt) |
| `POST /business/features/<id>/ai-import/check`, `.../ai-import` | kiểm tra, nhập file AI trả về |
| `GET /business/search?q=` | tìm trong nghiệp vụ và node |
| `GET /business/nodes/<node_id>` | thông tin node và các luồng có chứa node |
| `GET/POST /business/rules`, `GET/PATCH /business/rules/<id>` | quy tắc (`?status=&node=&domain=`); duyệt bằng `{"status":"approved"}` |
| `GET/POST /business/decisions`, `GET/PATCH /business/decisions/<id>` | quyết định |
| `GET/POST /business/questions`, `POST /business/questions/<id>/answer` | câu hỏi cho người |
| `GET /business/review-queue` | mọi thứ chờ người duyệt (bản nháp AI, quy tắc cần review, câu hỏi mở, phần tài liệu có code đã đổi) |
| `GET /business/kb-status` | độ tươi của graph, chất lượng extract, số mục chờ duyệt, thống kê gọi MCP và câu hỏi không có kết quả |
| `GET /business/owners`, `GET /business/ops-facts` | owner của system, dữ liệu vận hành |

Người duyệt lấy từ header `X-Code-Map-User` nếu có; duyệt quy tắc/quyết định và trả lời câu hỏi bắt buộc có header này.

Hiệu năng với schema lớn: backend bỏ node `COLUMN` và các cạnh cấu trúc khi
dựng index (một lần cho mỗi lần extract), cache cây theo thời điểm sửa của
`codemap-web.sqlite` và `knowledge.sqlite`, chỉ liệt kê 300 entry point chưa
gán đầu tiên (phần còn lại tìm bằng ô tìm kiếm); graph chỉ render node đang
nằm trong khung nhìn.

## Cấu trúc source

```text
extractor/
  code_tree_exporter/   # engine: package, contract, extractor runtimes
    extractors/_sql/    # CodeTree.Sql: ANTLR PL/SQL trên .NET
    extractors/sql-service/  # service JSON-lines cho extractor Python
    rag_export.py       # codemap-rag.sqlite (VS Code) + codemap-web.sqlite (web)
    serving_tables.py   # các bảng graph_* của web
  tests/                # python -m unittest discover -s extractor/tests -t extractor
  pyproject.toml, requirements.txt
web/
  backend/api/          # Flask API (python -m unittest discover -s web/tests -t .)
  frontend/             # React UI (src/business: tài liệu nghiệp vụ và trang node; src/code-flow: node graph, panel chi tiết)
  configs/, ecosystem.config.js (pm2), requirements.txt
vscode-extension/       # @codemap, tools cho Agent mode (npm test, npm run package)
shared/fixtures/        # dữ liệu kiểm thử dùng chung: token vectors Python ↔ TypeScript, graph mẫu
examples/
  sample-source/        # source demo
  input-data/           # CSV nghiệp vụ (từ điển, jobnet, tên JA/EN của bảng, ...)
  catalog/              # catalog demo cho engine
  demo-config.json
scripts/                # run_demo_pipeline.py
requirements.txt        # gộp extractor + web cho một virtualenv
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
