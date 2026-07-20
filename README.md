# Code Map

Code Map quét Oracle PL/SQL, XML SQL và SQL nhúng trong C# qua EF/Dapper/ADO.NET; import metadata bảng/cột; lưu graph/lineage vào SQLite; phục vụ qua Flask API + React UI.

## Cài đặt

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd webapp && npm install && npm run build && cd ..
```

## Cấu hình

Project dùng một file `code-map.config.json`:

- `db`: SQLite DB.
- `api`: Flask/PM2/UI runtime.
- `imports.csv`: metadata imports độc lập theo DB/schema/encoding/priority.
- `extractors.sources`: source roots, stable ID, repo, DB/schema, exclusions, rules.
- `extractors.state`: lease, heartbeat, rotating log.

Rule extractor/owner hợp lệ:

- `oracle_plsql` + `callable_or_file`.
- `xml_sql` + `file`.
- `csharp_sql` + `repository_or_project`.

C# map mỗi file tới `.csproj` gần nhất. Class `*Repository`/`*Dao` tạo owner `Repository`; class khác tạo owner `Application`. XML luôn tạo owner `SourceFile`.

Ví dụ rút gọn:

```json
{
  "db": "code_map.db",
  "imports": {
    "csv": [{
      "id": "hr-metadata",
      "path": "metadata/hr",
      "kind": "table_definitions",
      "db_name": "OracleHRDB",
      "schema": "HR",
      "encoding": "utf-8",
      "priority": 0
    }]
  },
  "extractors": {
    "state": {
      "log_path": "logs/extraction.log",
      "log_max_bytes": 10485760,
      "log_backups": 5
    },
    "sources": [{
      "id": "business-source",
      "path": "src",
      "repo": "business",
      "db_name": "OracleHRDB",
      "schema": "HR",
      "priority": 0,
      "exclude": ["**/bin/**", "**/obj/**", "**/.git/**"],
      "rules": [
        {"patterns": ["**/*.pkb", "**/*.sql"], "extractor": "oracle_plsql", "owner": "callable_or_file"},
        {"patterns": ["**/*.xml"], "extractor": "xml_sql", "owner": "file"},
        {"patterns": ["**/*.cs"], "extractor": "csharp_sql", "owner": "repository_or_project"}
      ]
    }]
  }
}
```

`extractors.reset`, `table_definitions_path`, source/import ID trùng, import roots overlap, rule không hợp lệ đều bị từ chối trước khi mở DB.

## Unified pipeline

Metadata luôn chạy trước source trong cùng một run/lease:

```bash
.venv/bin/python -m extractors.run_all --config code-map.config.json
```

Mặc định incremental:

- `stat()` fast path; không đọc file không đổi.
- SHA-256 xác nhận content khi stat/context thay đổi.
- File thành công commit facts + projection trong một transaction.
- File lỗi giữ facts/fingerprint tốt gần nhất; run sau retry.
- Full scan chỉ cleanup file thực sự mất sau khi scan source thành công.
- Một writer được bảo vệ bởi lease + fencing token + heartbeat.

Buộc parse lại file được discover, không truncate DB:

```bash
.venv/bin/python -m extractors.run_all --config code-map.config.json --rebuild
```

Incremental theo Git name-status manifest (`A`, `M`, `D`):

```bash
.venv/bin/python -m extractors.run_all --config code-map.config.json --files-from changed-files.txt
```

Theo dõi:

- Rotating log tại `extractors.state.log_path`.
- Run/status/counters/current file tại `extraction_runs`.
- Work/error/delete attempts tại `extraction_run_files`.
- Latest fingerprint/error tại `extraction_files`.

## Bootstrap v2 an toàn

Build full staging sibling, validate integrity/projections/API-read queries, checkpoint WAL, xuất `<db>.v2.ready`:

```bash
.venv/bin/python -m extractors.bootstrap_v2 --config code-map.config.json
```

Lệnh này không sửa, xóa hoặc rename live DB. Khi lỗi, `<db>.v2.tmp` bị xóa. Cutover chỉ thực hiện bằng controlled operation riêng: dừng API, backup live DB, atomic rename, restart, smoke test.

## CSV metadata

Header được nhận diện nghiêm ngặt; header lạ không được phép replace metadata thành rỗng. Encoding cấu hình explicit: `utf-8`, `utf-8-sig`, `cp932`, `shift_jis`, `euc_jp`.

Ví dụ:

```csv
table_code,table_name_ja,table_name_en,description,column_code,column_name_ja,column_name_en,column_description
EMPLOYEES,従業員,Employees,Employee master,EMPLOYEE_ID,従業員ID,Employee ID,Primary key
```

## API/UI local

```bash
CODE_MAP_CONFIG=code-map.config.json .venv/bin/python api/app.py
```

Mở `http://127.0.0.1:8000/code-map/`.

## Production PM2

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

## Kiểm thử

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
cd webapp && npm run build && cd ..
```

## Cấu trúc

- `common/`: config/path/source decoding.
- `db/`: entities, state/fact schema, transactional projection writer.
- `extractors/`: scanner, imports-first coordinator, PL/SQL/XML/C# handlers, staging bootstrap.
- `api/`: read-only serving layer; không chạy extraction.
- `webapp/`: React/Vite UI.
- `samples/`: fixtures/demo.
- `tests/`: extraction, state, metadata, coordinator, staging regressions.
