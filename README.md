# Code Map — chạy demo

Pipeline đọc code trong `demo-sources/`, tạo CSV graph trong `output/`, rồi insert vào SQLite.

Config nằm trực tiếp trong `configs/`:

- Sáu file `*-*.json` cấu hình standalone extractors, có trường `type`.
- `code-map.config.json` chỉ cấu hình backend API và SQLite.

## 1. Cài dependency

Yêu cầu: Python 3, Node.js, .NET SDK.

macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. CSV nằm ở đâu?

CSV có hai nhóm:

```text
input-data/                         # CSV nhập thủ công
  tables.csv                        # registry table authoritative
  tables/                           # mỗi table là một file columns
    ORDER_HEADER.csv
    ORDER_LINE.csv
    BACKORDER.csv
  jobnet.csv                        # job + thứ tự chạy
  executable-mappings.csv           # map job sang executable
  localized-metadata.csv            # metadata bổ sung

output/<loại>/<source>/             # CSV do extractor sinh
  manifest.json
  nodes.csv
  edges.csv
  evidence.csv
  issues.csv
```

`tables.csv` chứa metadata table:

```csv
database,table_code,table_name_ja,table_name_en
```

Tên child file dùng `table_code`. Mỗi file chỉ chứa columns. `relation_table` là `table_code` liên quan; để trống nếu không có:

```csv
column_code,column_name_ja,column_name_en,ordinal_position,data_type,nullable,note,relation_table
BACKORDER_ID,入荷待ち注文ID,Backorder ID,1,NUMBER,false,,
ORDER_ID,注文ID,Order ID,2,NUMBER,false,,ORDER_HEADER
```

## 3. Chạy toàn bộ pipeline

Từ thư mục project trên macOS:

```bash
.venv/bin/python scripts/run_demo_pipeline.py --fresh
```

Trên Windows PowerShell:

```powershell
.venv\Scripts\python.exe scripts\run_demo_pipeline.py --fresh
```

Script thực hiện:

1. Extract Angular.
2. Extract .NET API.
3. Extract .NET batch.
4. Extract Oracle PL/SQL.
5. Extract SQL files.
6. Validate mọi CSV package trong `output/`.
7. Insert CSV thủ công và CSV graph vào SQLite.
8. Chạy `PRAGMA integrity_check`.

Kết quả:

```text
data/code-flow-demo.sqlite
```

`--fresh` xóa DB demo cũ trước khi import. Bỏ option này để cập nhật DB hiện có.

## 4. Kiểm tra dữ liệu

Nếu máy có `sqlite3`:

```bash
sqlite3 data/code-flow-demo.sqlite "SELECT node_type, COUNT(*) FROM graph_nodes GROUP BY node_type ORDER BY node_type;"
```

Table và column được insert vào:

```text
graph_nodes
 table_details
 column_details
```

## 5. Lỗi thường gặp

- `Missing authoritative CSV files`: kiểm tra `input-data/tables/*.csv` và ba file CSV còn lại trong `input-data/`.
- `Missing required command: node`: cài Node.js.
- `Missing required command: dotnet`: cài .NET SDK.
- Validation lỗi: xem package tương ứng trong `output/` và file `issues.csv`.
