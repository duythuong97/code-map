# Code Map Graph RAG cho VS Code

Hỏi về nghiệp vụ của codebase ngay trong VS Code Chat. Extension đọc
`codemap-rag.sqlite` do bước extract của Code Map sinh ra. File này gồm graph
màn hình → API → code → PL/SQL → bảng, các job batch và từ điển nghiệp vụ.
Extension đưa các dữ kiện liên quan (kèm `file:dòng`) cho model chat đang dùng
trong VS Code (Copilot Chat, Claude, …), nên không cần API key riêng.

## Cài

```bash
cd vscode-extension
npm install
npm run package          # tạo codemap-graph-rag.vsix
code --install-extension codemap-graph-rag.vsix
```

Cần VS Code 1.95+ và một model chat (ví dụ GitHub Copilot Chat).

## Tạo dữ liệu

Bước extract tự ghi `codemap-rag.sqlite` cạnh `graph.sqlite`:

```bash
python -m code_tree_exporter extract --config my-config.json
```

Dữ liệu nghiệp vụ được đọc từ thư mục `inputData` trong config:

| File | Dùng để |
|---|---|
| `semantic-dictionary.csv` | Thuật ngữ nghiệp vụ (vi/en/ja), định nghĩa, bí danh trong code |
| `tables.csv`, `tables/<TABLE>.csv` | Tên bảng/cột tiếng Nhật, tiếng Anh |
| `localized-metadata.csv` | Tên bổ sung |
| `jobnet.csv`, `executable-mappings.csv` | Job batch → chương trình chạy |

Nếu đã có `graph.sqlite` thì không cần extract lại:

```bash
python -m code_tree_exporter rag-export --graph output/demo --input-data input-data
```

Trong VS Code cũng có lệnh **Code Map: Run Extract**. Lệnh này chạy extract
rồi tự nạp lại graph.

## Dùng

Trong Chat:

- `@codemap hủy đơn hàng xử lý ở đâu, ghi vào bảng nào?`
- `@codemap /impact ORDER_HEADER`: ai đọc, ghi, phụ thuộc vào bảng này.
- `@codemap /flow POST /api/orders`: luồng từ API (hoặc màn hình, job) xuống dữ liệu.
- `@codemap /explain`: vai trò nghiệp vụ của file đang mở (hoặc đoạn đang chọn).
- `@codemap /gaps`: thuật ngữ chưa nối được với code, job không tìm thấy chương trình, lỗi extractor.

Câu trả lời trích dẫn dữ kiện `[N3]`, `[E2]`, `[C1]`, có danh sách tham chiếu mở
thẳng tới dòng code.

Trong Agent mode, model tự gọi tool `#codemap` (truy vấn graph) và
`#codemapFile` (dữ kiện của một file) khi cần.

## Cài đặt

| Setting | Ý nghĩa |
|---|---|
| `codemap.databasePath` | Đường dẫn `codemap-rag.sqlite`. Để trống: lấy file mới nhất trong workspace |
| `codemap.sourceRoot` | Thư mục gốc của các đường dẫn evidence. Để trống: lấy root lúc extract, rồi tới workspace |
| `codemap.extract.configPath` / `enginePath` / `python` | Cho lệnh Run Extract |
| `codemap.retrieval.maxNodes` / `hops` / `maxContextChars` | Kích thước ngữ cảnh gửi cho model |

## Cách hoạt động

1. **Tìm điểm bắt đầu.** Câu hỏi được tách token: bỏ dấu tiếng Việt, tách
   camelCase và snake_case, tiếng Nhật thành cặp ký tự. Token được so với:
   - từ điển nghiệp vụ: nhãn "Hủy đơn hàng", bí danh `cancelOrder`;
   - chỉ mục tên node.

   Kết quả được xếp hạng theo độ hiếm của từ và loại node.
2. **Đi graph** 2–4 bước. Cạnh ghi/đọc dữ liệu, gọi API và job được ưu tiên.
   Mỗi node chỉ lấy một số cạnh giới hạn, nên node nối với rất nhiều node khác
   không làm phình ngữ cảnh.
3. **Gửi dữ kiện cho model**, mỗi dữ kiện đánh số, kèm luồng dữ liệu đã tính
   sẵn và các lỗi extractor. Model chỉ được trả lời từ các dữ kiện này.

Extension đọc một bản sao của file sqlite. Vì vậy lần extract sau vẫn ghi đè
được thư mục output, kể cả trên Windows. Nếu runtime có `node:sqlite` thì dùng
nó; nếu không thì dùng `sql.js` (WASM).
