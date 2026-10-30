# Kiến trúc Code Map

Tài liệu cho người sửa hoặc mở rộng Code Map: các lớp, dữ liệu đi qua đâu, và
thêm một loại source, một bước nối hay một VCS mới thì sửa ở đâu. Cách cài và
chạy ở [README](../README.md), config và lệnh ở [REFERENCE](REFERENCE.md).

## Các lớp

```text
             web/backend (Flask: route REST, mcp_http.py)      codemap.mcp (MCP: tools, prompts, stdio)
                            \                                   /
                             codemap   (lớp đọc, không Flask: settings, storage, knowledge,
                                        business, retriever, workspace, kb, cli)
                                |
                             code_tree_exporter   (extractor; contract/ là phần lớp đọc dùng chung)
```

Ba package Python (`code_tree_exporter` trong `extractor/`, `codemap`, `web`) được
cài một lần bằng [pyproject.toml](../pyproject.toml) ở chế độ editable
(`requirements.txt` kết thúc bằng `-e .`), nên không có `sys.path`/`PYTHONPATH`
nào trong code. Import chỉ đi xuống. `codemap/tests/test_layers.py` kiểm tra điều này: một
import đi ngược chiều sẽ làm test fail. Ngoại lệ duy nhất là `codemap/cli.py`,
nơi nối mọi lớp thành một lệnh.

## Dữ liệu đi qua đâu

```text
config (một file, xem REFERENCE "Cấu hình chung")
  │
  ▼
pipeline ── mỗi source một extractor (subprocess) ──▶ package CSV (contract/csv-contract.md)
  │           angular (.mjs, TypeScript API) · dotnet-api/batch (Roslyn C#)
  │           oracle-plsql · sql-files · xml-sql (Python + CodeTree.Sql)
  ▼
merge ─▶ linker.RESOLVERS (theo thứ tự) ─▶ hierarchy ─▶ graph.sqlite   (dữ liệu gốc, tên của engine)
                                                          │
                                                          ▼
                                     serving.py ─▶ codemap.sqlite
                                                    ├─ graph_*   (serving_tables.py: tên phục vụ, evidence, tên ja/en, nghiệp vụ)
                                                    └─ search_*  (search_index.py: chỉ mục dựng TỪ graph_*)
                                                          │
                                          ┌───────────────┴───────────────┐
                                        web                             MCP
                                          └──── knowledge.sqlite (do người viết, không bị extract ghi đè) ────┘
```

**Hai file output, mỗi file có một lý do riêng:**
- `graph.sqlite` là dữ liệu gốc, đầy đủ và nặng (`semantic_tree` có thể vài MB
  mỗi node). diff, stale và incremental đọc file này.
- `codemap.sqlite` là bản đọc duy nhất cho web và MCP. Chỉ mục tìm kiếm của
  MCP được dựng từ chính các bảng `graph_*` trong cùng file, nên hai bên luôn
  thấy cùng node và cùng tên edge. `extractor/tests/test_serving_export.py`
  kiểm tra điều này.
- `knowledge.sqlite` tách riêng vì là dữ liệu do người viết.

**Một bộ tên gọi cho bên đọc.** Engine giữ tên cũ (`READS_FROM`, `WRITES_TO`,
`HANDLES_API`). [contract/vocabulary.py](../extractor/code_tree_exporter/contract/vocabulary.py)
là nơi duy nhất đổi chúng sang tên phục vụ (`READS`, `INSERTS`/`UPDATES`/…,
`HANDLED_BY`), và cũng là nơi duy nhất khai báo các nhóm edge đọc, ghi, gọi,
lần theo flow. Code đọc `codemap.sqlite` dùng các nhóm phục vụ; code đọc
thẳng `graph.sqlite` (diff, publisher) dùng các nhóm `ANY_*`, nhận cả hai bộ tên.

**Một quy tắc lần theo flow.** Đi từ một entry (màn hình, API, job) tới dữ liệu nó
đọc/ghi đều dùng `vocabulary.follows()`: các bước nối (`FLOW_STEP_EDGES`, cả tên
engine lẫn tên phục vụ) và `CONTAINS` chỉ khi đi vào phần chạy được
(`FLOW_PART_TYPES`: UI action, job, SQL của method, routine của package). Bốn nơi
dùng chung quy tắc này:
- flow tính sẵn của engine ([v3/publisher.py](../extractor/code_tree_exporter/v3/publisher.py)
  → `graph_paths`, `search_flows`);
- trang code-flow của web và phần "Data read or written" của `codemap_trace_flow`
  ([codemap/flows.py](../codemap/flows.py));
- tài liệu nghiệp vụ (`codemap/business.py`, `GraphIndex.reach`).

`codemap/tests/test_flows.py` kiểm tra rằng các nơi này cho cùng một tập bảng.
Ngoại lệ duy nhất: job lấy từ `jobnet.csv` được thêm vào sau khi engine chạy,
nên flow tính sẵn của engine không có chúng. Web, MCP và tài liệu nghiệp vụ thì có.

## Điểm mở rộng

| Muốn thêm | Sửa ở đâu | Không cần sửa |
|---|---|---|
| Một loại source mới (ví dụ CloudFormation của API Gateway) | một extractor trong `extractors/` + `_SPECS` của [extractors/__init__.py](../extractor/code_tree_exporter/extractors/__init__.py); xuất package CSV | pipeline, web, MCP |
| Một kiểu nối giữa các source (route gateway → API, base URL lấy từ config) | một hàm trong `RESOLVERS` của [linker.py](../extractor/code_tree_exporter/linker.py), đặt trước `"api"` nếu bước nối API cần nó | extractor của source khác |
| Một loại node/edge mới | `contract/graph_contract.py`; nếu là đọc/ghi/gọi thì thêm vào nhóm trong `vocabulary.py` | web, MCP: tự nhận qua các nhóm |
| Một VCS mới | một lớp con của `Repository` trong [vcs.py](../extractor/code_tree_exporter/vcs.py) + `_KINDS` | revisions, stale, checkout |
| Một tool MCP | `codemap/mcp/tools.py`, đọc qua `codemap.retriever` / `codemap.storage` | extractor |
| Một màn hình/API web | `web/backend/api/` + `web/frontend/`, dùng `codemap.*` | MCP |

## Message queue (Kafka…): khai báo bằng `message-flows.csv`

Code gửi và code nhận một message nằm ở hai source khác nhau, broker che mất
liên kết giữa chúng. Vì vậy liên kết được khai báo trong
`inputData/message-flows.csv` ([messages.py](../extractor/code_tree_exporter/messages.py)):

```csv
topic,kind,producer,consumer,repository,note
order-created,kafka,OrderService.CreateOrder,,,
order-created,kafka,,OrderFulfillment.exe,,
```

- `kind` mặc định là `kafka`. Mỗi dòng ghi một quan hệ: có producer, có
  consumer, hoặc cả hai.
- `producer` / `consumer` là thứ graph đã có:
  - method: `Class.Method` hoặc `Namespace.Class.Method`;
  - API: `POST /api/orders`;
  - executable: `OrderFulfillment.exe` (khớp cả tên project, assembly và alias,
    bỏ qua `-`, `_`, `.` và hoa/thường).
- Tên trùng ở nhiều repo thì ghi thêm cột `repository`.
- Kết quả là node `MESSAGE_TOPIC` với `producer -PUBLISHES-> topic` và
  `topic -CONSUMED_BY-> consumer`. Hai edge này là bước của flow (`CALL_EDGES`),
  nên web, MCP, tài liệu nghiệp vụ và flow tính sẵn của engine đi tiếp từ API
  gửi message tới bảng mà consumer ghi.
- Dòng không khớp gì ra issue `MESSAGE_ENDPOINT_NOT_FOUND`, khớp nhiều hơn một
  ra `AMBIGUOUS_SYMBOL`. Issue nằm trong output, nên sau mỗi lần extract cần xem
  lại file CSV.

Tiếp theo (cần mẫu code): nhận diện `ProduceAsync` / `Consume` của Confluent.Kafka
trong Roslyn, với tên topic lấy từ config.

## Source cũ: Angular 4, .NET Framework, SVN, Windows

- **Angular 2–5:** extractor đọc `.angular-cli.json` (`apps[].root`) và `Http`
  của `@angular/http` bên cạnh `HttpClient`. TypeScript 5.9 parse được cú pháp
  TS 2.x. `codemap init` nhận diện `.angular-cli.json`.
- **.NET Framework:** `MSBuildWorkspace` mở csproj kiểu cũ khi máy có Build
  Tools for Visual Studio (workload web, để có `Microsoft.WebApplication.targets`).
  Nếu không có, extractor phân tích từng file `.cs` rời (`FallbackFileCount`),
  semantic kém hơn. Web API 2 (`MapHttpRoute`, `[RoutePrefix]`) và key trong
  `Web.config` đã được đọc.
- **SVN:** `vcs.SvnRepository` ghi revision, `^/branch`, file đã sửa; đếm số
  revision mới và `extract --at` qua `svn log`/`svn export`. Stale check dựa trên
  sha256 từng file nên không cần server. `codemap init` nhận diện `.svn`.
- **Windows:** `.env` và config đọc nguyên văn và chịu BOM. Nên viết `/` trong
  đường dẫn. `codemap.cmd` là lệnh chính. PM2 tự chọn `.venv\Scripts\python.exe`.

## Việc tiếp theo (chưa làm)

Các việc dưới đây là tính năng, chạy trên các điểm mở rộng ở trên, và đều cần
mẫu code thật trước khi viết:

1. **Giá trị config thành dữ liệu của graph.** Node `CONFIG_KEY` mang thêm
   value (`Web.config` appSettings, `Web.<env>.config`, `appsettings*.json`,
   `config.json`, `environment*.ts`). Extractor xuất lệnh gọi API dạng
   `{base: configKey, path}`, một resolver ghép lại.
2. **API Gateway: phần còn thiếu.** REST API (`AWS::ApiGateway::*`) đã làm,
   xem mục dưới. Còn `AWS::ApiGatewayV2::*` (HTTP API), SAM `Events`, route
   `{proxy+}` (một route gateway cho cả một nhóm API) và `BasePathMapping`/stage.
3. **Lệnh gọi HTTP ra ngoài từ .NET** (`HttpClient`, `HttpRequestMessage`,
   Refit/RestSharp nếu có), dùng lại 1 và 2.

## API Gateway (REST API trong CloudFormation)

Source `"type": "api-gateway"`, `folders` là các template YAML/JSON
([extractors/api_gateway/runner.py](../extractor/code_tree_exporter/extractors/api_gateway/runner.py)).

- Mỗi `AWS::ApiGateway::Method` (trừ `OPTIONS` và integration `MOCK`) thành một
  `API_OPERATION` có `gateway: true`. Path công khai là chuỗi `PathPart` đi từ
  `ResourceId` ngược lên theo `ParentId` tới root. Root là cha không phải một
  resource trong các template của source này (`!GetAtt Api.RootResourceId`,
  hoặc giá trị import từ stack khác). Resource có thể khai báo ở template khác
  trong cùng source.
- `Integration.Uri` (`!Sub`, `Fn::Join` hay chuỗi) được bỏ phần
  `scheme://host:port`; phần path còn lại cùng `IntegrationHttpMethod` là
  `integration` của route.
- Linker `gateway` nối route gateway `ROUTES_TO` API backend bằng suffix dài nhất
  của path integration. Phần prefix của backend (thư mục ảo, tên service) không
  cần khai báo. Không tìm thấy hoặc khớp nhiều hơn một thì ra issue
  `GATEWAY_BACKEND_NOT_FOUND` / `AMBIGUOUS_API_LINK`.
- `link_api` cũng chọn **suffix dài nhất**. Màn hình gọi
  `https://<gateway>/<stage>/a/b/c` sẽ khớp route gateway `/a/b/c` trước API
  backend `/b/c`. Hai API cùng tên route nhưng khác prefix không còn bị coi là
  mơ hồ khi URL gọi có prefix.
- Flow `màn hình → CALLS_API → route gateway → ROUTES_TO → API backend → …` được
  web, MCP và tài liệu nghiệp vụ đi qua như mọi bước gọi khác.
