# corpus-gen: đo chất lượng khi không có ticket

Sinh một hệ thống giả lập nhiều system mà **mọi phụ thuộc đều đã biết trước**,
rồi chấm điểm graph và các bản phân tích ảnh hưởng trên đó.

Mỗi domain (invoice, shipment, …) đi qua 5 system: Angular (`web`) → ASP.NET
(`api-*`) → PL/SQL (`db`) → bảng; SQL report và SQL*Loader (`ops`); batch .NET
(`batch`) chạy theo `--mode`, khởi động từ `jobnet.csv`. Quy mô: `s` (3 domain),
`m` (12), `l` (60).

Mỗi phụ thuộc được sinh bởi một **pattern** và ghi vào `truth/edges.jsonl` cùng
kỳ vọng:

| expected | nghĩa | khi graph thiếu |
|---|---|---|
| `detectable` | phân tích tĩnh phải thấy | **regression**, test fail |
| `partial` | chỉ cần báo issue (ví dụ `EXECUTE IMMEDIATE` với chuỗi trong biến) | phải có issue trên node |
| `undetectable` | điểm mù đã biết (tên bảng ghép chuỗi lúc chạy) | phải có issue; im lặng là lỗi |

Danh sách pattern nằm trong `corpus_gen/generator.py` (`PATTERNS`), sinh ra
`truth/patterns.json`.

```bash
python tools/corpus-gen generate --out /tmp/corpus --size m --git
python -m code_tree_exporter extract --config /tmp/corpus/config.json
python tools/corpus-gen score-extract --corpus /tmp/corpus                     # bài toán A: extractor
python -m code_tree_exporter verify-oracle --graph /tmp/corpus/out --dependencies /tmp/corpus/truth/all_dependencies.csv
python tools/corpus-gen baseline --corpus /tmp/corpus                          # bài toán B: graph, không LLM
python tools/corpus-gen score-impact --corpus /tmp/corpus --scenario invoice-cancel-rule --answer impact.yaml
python tools/corpus-gen apply-scenario --corpus /tmp/corpus --scenario invoice-cancel-rule   # sửa code + commit, để thử diff
```

- `score-extract --sources web,db,ops` chỉ chấm phần không cần .NET.
- `scenarios/*.yaml` là yêu cầu thay đổi kèm ảnh hưởng thật (đi ngược trong
  truth graph tới màn hình và job). Chấm `impact.yaml` của agent bằng
  `score-impact`. Mỗi mục bỏ sót được phân loại: graph *có* phụ thuộc đó (lỗi ở
  retrieval hoặc ở agent) hay graph *không có* (lỗi ở extractor).
- `baseline` chấm chính graph (đi ngược từ node bị đổi), không dùng LLM: đó là
  trần của mọi agent dùng Code Map.

`extractor/tests/test_corpus_gen.py` chạy corpus `s` qua extractor thật. Đây là
test đã bắt được lỗi mất lineage trong package PL/SQL: tham chiếu bảng bị khử
trùng lặp theo cả file, nên procedure thứ hai đụng cùng một bảng bị mất cạnh.
`verify-oracle` không bắt được lỗi này, vì Oracle chỉ ghi phụ thuộc ở mức
package. Vì vậy cần cả hai cách đo.

**Giới hạn:** corpus do cùng nhóm viết extractor tạo ra, nên có nguy cơ cùng
điểm mù. Hãy bổ sung pattern từ code thật (đã ẩn danh), do người khác viết, và
viết pattern trước khi xem extractor hỗ trợ gì.
