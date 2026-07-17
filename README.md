# Code Map

Code Map quét source PL/SQL, import metadata bảng/cột, lưu kết quả vào SQLite và hiển thị graph/lineage qua Flask API + React UI.

## Cài đặt

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd webapp && npm install && npm run build && cd ..
```

## Cấu hình

Project dùng **một file config duy nhất**: `code-map.config.json`.

- `db`: SQLite DB dùng chung cho API và extractor.
- `api`: runtime config cho Flask/PM2/UI.
- `extractors`: config import metadata và extract source.

Ví dụ:

```json
{
  "db": "code_map.db",
  "api": {
    "url_prefix": "/code-map",
    "host": "127.0.0.1",
    "port": 8000,
    "pm2_name": "code-map",
    "python": "python3",
    "python_no_site": true,
    "venv_python": "python3.13"
  },
  "extractors": {
    "reset": true,
    "db_name": "OracleHRDB",
    "table_definition": {
      "db_name": "OracleHRDB",
      "schema": "HR"
    },
    "table_definitions_path": "samples/table_definitions",
    "sources": [
      {"path": "samples", "repo": "samples", "schema": "HR"}
    ]
  }
}
```

Sửa `code-map.config.json`; không sửa hardcode trong code.

## Lệnh chính

Import metadata + extract PL/SQL vào cùng SQLite DB:

```bash
.venv/bin/python -m extractors.run_all --config code-map.config.json
```

`extractors.reset: true` chỉ xóa graph `nodes/edges`; không xóa metadata `table_definitions/table_columns` vừa import.

Chỉ extract PL/SQL, không import metadata:

```bash
.venv/bin/python -m extractors.run_extract --config code-map.config.json
```

Chỉ import CSV metadata:

```bash
.venv/bin/python -m extractors.import_csv samples/table_metadata.csv --config code-map.config.json
```

CSV mẫu: `samples/table_metadata.csv`.

Header hỗ trợ:

```csv
mã table,tên tiếng nhật,tên tiếng anh,description,mã column,tên tiếng nhật column,tên tiếng anh column,description column
EMPLOYEES,従業員,Employees,Employee master,EMPLOYEE_ID,従業員ID,Employee ID,Primary key
```

Encoding hỗ trợ: `utf-8-sig`, `utf-8`, `cp932`, `shift_jis`, `euc_jp`.

## Chạy API/UI local

```bash
CODE_MAP_CONFIG=code-map.config.json .venv/bin/python api/app.py
```

Mở:

```text
http://127.0.0.1:8000/code-map/
```

## Chạy production bằng PM2

```bash
pm2 delete code-map || true
pm2 start ecosystem.config.js --update-env
pm2 save
pm2 logs code-map --lines 80
```

Smoke test:

```bash
curl -fsS http://127.0.0.1:8000/code-map/app-config.js
curl -fsS http://127.0.0.1:8000/code-map/api/graph-contract
```

PM2 trên `/Volumes` dùng `python3 -S -c ...` để tránh lỗi quyền đọc `.venv/pyvenv.cfg`.

## IIS subdirectory

App hỗ trợ chạy dưới `/code-map` qua `api.url_prefix` trong `code-map.config.json`.

Frontend đọc prefix từ `/code-map/app-config.js`; không hardcode trong React.

## Kiểm thử / validation

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m extractors.run_all --config code-map.config.json
cd webapp && npm run build && cd ..
```

Coverage hiện tại của feature matrix PL/SQL: khoảng `25/30 = 83.3%`.

## Cấu trúc project

- `code-map.config.json`: config duy nhất cho API + extractor.
- `api/`: Flask API + static UI; chỉ đọc DB/phục vụ API, không chứa logic extract/import pipeline.
- `db/`: SQLite schema, entity model, writer/migration.
- `extractors/`: toàn bộ data init/import/extract/pipeline và PL/SQL extractors.
- `common/`: helper config/path và đọc source text an toàn encoding.
- `samples/`: PL/SQL sample + metadata sample để test/demo.
- `tests/`: unit test và feature coverage matrix.
- `webapp/`: React/Vite UI.
- `ecosystem.config.js`: PM2 runtime config.

## Ghi chú kiến trúc

- API đọc từ SQLite và trả JSON/UI; không chạy extractor trong `api/`.
- Extractor hiện là regex-based practical extractor, chưa phải full PL/SQL parser.
- Extractor đang cover các nhóm chính: package/procedure/function/trigger, DML table refs, sequence, package call, constants/variables/types đơn giản, simple dynamic SQL và column lineage phổ biến.
