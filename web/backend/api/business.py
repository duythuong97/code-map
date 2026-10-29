"""Business layer over the graph: menus, features, flows and their documents.

The tree is derived from the graph on every request so it follows the latest
extract:

* every ``SCREEN`` (Angular route) is a feature, grouped into a menu by the
  first segment of its route;
* every ``JOB_NETWORK`` (jobnet.csv) is a feature in the "Chạy nền" menu;
* a feature proposes one flow per ``UI_ACTION`` (screen) or ``JOB`` (jobnet);
* ``API_OPERATION`` nodes no feature reaches are listed as unassigned entry
  points.

What people change (names, descriptions, review status, flow documents written
by AI and edited by hand) is kept in the knowledge database (``kn.biz_*``) and
keyed by graph ids, so the next extract does not overwrite it.

AI never sees the code directly. ``export_pack`` writes one YAML file per
feature with the feature's nodes, edges, code evidence and a template to fill;
``check_import``/``apply_import`` read the file the AI returns, check every node
reference against the graph and keep reviewed parts untouched.
"""
from __future__ import annotations

import hashlib
import uuid
import json
import re
import sqlite3
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

PART_STATUSES = ("ai", "edited", "approved")
RULE_STATUSES = ("ok", "warn", "bad")
BATCH_MENU = "menu:batch"
UNSORTED_MENU = "menu:unsorted"
STRANDED_MENU = "menu:stranded"
MAX_FLOW_NODES = 150
MAX_FEATURE_NODES = 400
MAX_UNASSIGNED = 300
MAX_EVIDENCE_CHARS = 600
MAX_CONDITIONS_PER_NODE = 12

# Edges that carry a feature from its entry down to the data it touches.
FOLLOW_EDGES = frozenset({
    "BELONGS_TO", "CALLS", "CALLS_API", "HANDLED_BY", "STARTS", "TRIGGERS", "TRIGGERS_JOB", "EXECUTES_SQL",
    "RESOLVES_TO", "ROUTES_TO", "ENTRY_IN", "READS", "REMOTE_READS", "INSERTS", "UPDATES", "DELETES", "MERGES",
    "WRITES", "LOADS_INTO",
})
# CONTAINS is structural (project → screen, table → column); only a screen's
# UI actions and a jobnet's jobs belong to the feature.
CONTAINS_FROM = frozenset({"SCREEN", "JOB_NETWORK"})
DATA_TYPES = frozenset({"TABLE", "VIEW", "MATERIALIZED_VIEW", "COLUMN", "EXTERNAL_DATABASE_OBJECT", "SEQUENCE", "DATA_FILE"})
TERMINAL_TYPES = DATA_TYPES | {"EXTERNAL_API_OPERATION", "UNRESOLVED_REFERENCE", "EXTERNAL_SYSTEM"}
WRITE_EDGES = frozenset({"INSERTS", "UPDATES", "DELETES", "MERGES", "WRITES", "LOADS_INTO"})
READ_EDGES = frozenset({"READS", "REMOTE_READS"})
CONDITION_KINDS = frozenset({"branch", "case", "catch", "raise", "loop", "try"})

# Short keys used in YAML node references ("api: POST /api/orders").
REF_KEYS = {
    "SCREEN": "screen", "UI_ACTION": "ui_action", "API_CALL_REFERENCE": "api_call", "API_OPERATION": "api",
    "METHOD": "method", "LOCAL_ROUTINE": "method", "CONTROLLER": "controller", "SERVICE": "service",
    "REPOSITORY": "repository", "PROCEDURE": "procedure", "FUNCTION": "function", "TRIGGER": "trigger",
    "PLSQL_PACKAGE": "package", "SQL_FILE": "sql_file", "INLINE_SQL": "sql", "TABLE": "table", "VIEW": "view",
    "MATERIALIZED_VIEW": "view", "EXTERNAL_DATABASE_OBJECT": "external", "EXTERNAL_API_OPERATION": "external_api",
    "JOB_NETWORK": "jobnet", "JOB": "job", "EXECUTABLE": "executable", "COMMAND_MODE": "command",
    "UNRESOLVED_REFERENCE": "unresolved",
}

INSTRUCTIONS = """\
Bạn là chuyên viên phân tích nghiệp vụ (BA) kiêm lập trình viên. Nhiệm vụ: viết tài liệu nghiệp vụ cho
một tính năng của hệ thống, dựa hoàn toàn vào dữ liệu trong phần "Dữ liệu" (khối YAML `context`) ở cuối file
này. Bạn không có quyền truy cập code hay database nào khác. Kết quả trả về theo đúng phần "Định dạng trả về".

# Dữ liệu bạn có
- `context.feature`: tên, route/jobnet, điểm vào và mô tả hiện có của tính năng.
- `context.nodes`: các node code của tính năng. `ref` có dạng `loại: tên` (vd `api: POST /api/orders`,
  `method: OrderController.Create`, `procedure: PKG_ORDER.CREATE_ORDER`, `table: ORDER_HEADER`).
  Có thể kèm `file` (file:dòng), `term` (thuật ngữ nghiệp vụ đã duyệt), `ja`/`en` (tên tiếng Nhật/Anh),
  `conditions` (nhánh IF/CASE/EXCEPTION/RAISE trong code kèm dòng) và `comments` (comment trong code).
- `context.edges`: `[nguồn, loại cạnh, đích]`. CALLS/CALLS_API/HANDLED_BY = gọi; INSERTS/UPDATES/DELETES/
  MERGES = ghi bảng; READS/REMOTE_READS = đọc bảng (REMOTE qua DB link).
- `context.evidence`: đoạn code thật của một số node, kèm vị trí `at` (file:dòng).
- `context.answered`: câu hỏi đã được người dùng trả lời. Coi câu trả lời là sự thật, không hỏi lại.

# Việc cần làm
Điền vào khối YAML ở phần "Định dạng trả về" (`flows`). Mỗi flow là một luồng nghiệp vụ, đã được hệ thống chia sẵn thành các bước theo
hệ thống mà luồng đi qua (màn hình → API → database …); mỗi bước đã có sẵn danh sách `nodes`.
- `name` của flow: tên nghiệp vụ ngắn, dạng động từ + đối tượng (vd "Tạo đơn hàng"), không dùng tên kỹ thuật.
- `summary.text`: 1–3 đoạn. Ai làm, để làm gì, kết quả nghiệp vụ là gì, luồng này không bao gồm việc gì.
- Mỗi `steps[]`:
  - `name`: việc xảy ra ở bước này, theo góc nhìn nghiệp vụ (vd "Kiểm tra dòng đơn và tính tiền").
  - `actor`: người hoặc hệ thống thực hiện (vd "Nhân viên bán hàng", "Hệ thống", "Batch hằng đêm").
  - `text`: 1–3 đoạn mô tả bước làm gì, đọc/ghi dữ liệu nào, theo thứ tự xảy ra. Trích dẫn node bằng `[[tên]]`.
  - `rules`: các quy tắc/điều kiện nghiệp vụ thấy được trong `conditions`, `evidence` hoặc `comments`. Mỗi rule gồm
    `title` (một câu khẳng định, vd "Đơn mới luôn ở trạng thái PENDING"), `at` (tên node chứa điều kiện),
    `evidence` (file:dòng lấy từ context), `status` và `text` (giải thích ngắn, nêu khi không đạt thì sao).
    `status`: `ok` = code làm đúng như câu khẳng định; `warn` = có dấu hiệu nhưng chưa chắc (vd chỉ thấy trong comment);
    `bad` = ý đồ và code lệch nhau (vd thủ tục kiểm tra có thân rỗng).
- `edge_cases.items`: các trường hợp đặc biệt/lỗi suy ra từ code (dữ liệu rỗng, ngoại lệ, DB link lỗi …), mỗi mục một câu.
- `questions`: những điều code không đủ để kết luận; mỗi câu hỏi phải trả lời được bằng có/không hoặc một giá trị cụ thể.

# Quy tắc bắt buộc
1. Chỉ điền các trường đang trống. Không sửa phần có `locked: true` (người đã duyệt hoặc sửa tay); đọc chúng để hiểu ngữ cảnh.
2. Không sửa `export`. Giữ nguyên `id` của flow và step, số bước và danh sách `nodes`.
3. Trích dẫn node bằng `[[tên]]`, trong đó `tên` là phần sau dấu `:` của một `ref` trong `context.nodes`
   (vd `[[PKG_ORDER.CREATE_ORDER]]`). Không trích dẫn node không có trong danh sách, không bịa tên bảng/hàm.
4. Không khẳng định điều không có trong context. Chỗ nào không chắc, viết thành một câu trong `questions`.
5. Rule nào không có `evidence` sẽ bị hệ thống đánh dấu "Cần xác nhận"; đừng bịa vị trí.
6. Viết tiếng Việt, câu ngắn, chủ động. Giữ nguyên tên kỹ thuật (bảng, API, giá trị như 'PENDING').
   Các đoạn cách nhau bằng một dòng trống; có thể dùng **đậm** và `code`.
7. Chỉ trả về MỘT khối YAML hợp lệ: đúng khối ở phần "Định dạng trả về" (`export` và `flows`) đã điền, cùng cấu
   trúc và thứ tự. Không chép lại `context` hay phần hướng dẫn, không thêm lời giải thích ngoài YAML.
   Dùng block `|` cho các đoạn văn nhiều dòng.

# Ví dụ một bước đã điền
- id: s2
  nodes: [...giữ nguyên...]
  name: Kiểm tra dòng đơn và tính tiền
  actor: Hệ thống
  text: |
    [[OrderService.CreateOrder]] chuyển yêu cầu xuống [[PKG_ORDER.CREATE_ORDER]]. Trước khi ghi dữ liệu,
    procedure gọi [[PKG_ORDER.VALIDATE_LINES]] để kiểm tra dòng hàng của khách.
  rules:
  - title: Dòng đơn phải hợp lệ trước khi tạo đơn
    at: PKG_ORDER.VALIDATE_LINES
    evidence: order-db/order_flow.sql:62
    status: bad
    text: Thân procedure chỉ có NULL nên không dòng nào bị từ chối.
"""



class _BlockDumper(yaml.SafeDumper):
    """Multi-line text (instructions, paragraphs) is written as a readable `|` block."""


def _str_block(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_BlockDumper.add_representer(str, _str_block)

KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS kn.biz_items(item_id TEXT PRIMARY KEY,parent_id TEXT,name TEXT,description TEXT,
  status TEXT CHECK(status IS NULL OR status IN('ai','edited','approved')),reviewed_by TEXT,sort_order INTEGER,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS kn.biz_flows(flow_id TEXT PRIMARY KEY,feature_id TEXT NOT NULL,doc_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS kn.biz_exports(export_id TEXT PRIMARY KEY,feature_id TEXT NOT NULL,graph_hash TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""


class BusinessError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


# --------------------------------------------------------------------------- graph index

class GraphIndex:
    """Nodes and outgoing edges of codemap-web.sqlite, loaded once per extract."""

    def __init__(self, db: sqlite3.Connection) -> None:
        # Columns are left out: a large schema has hundreds of thousands of them and no flow walks through them.
        # Only screens need their properties (the route), so other rows skip the JSON parse.
        self.nodes: dict[str, dict[str, Any]] = {}
        self.by_type: dict[str, list[str]] = {}
        for row in db.execute(
            "SELECT node_id,node_type,technical_name,qualified_name,default_display_name,"
            "CASE WHEN node_type='SCREEN' THEN properties_json ELSE NULL END,system_key,database_key,repository_key "
            "FROM graph_nodes WHERE node_type<>'COLUMN'"
        ):
            node = {
                "node_id": row[0], "node_type": row[1], "technical_name": row[2] or "", "qualified_name": row[3] or "",
                "display_name": row[4] or row[2] or row[0], "properties": _json(row[5], {}),
                "system": system_of(row[1], row[6], row[7], row[8], row[0]),
            }
            self.nodes[row[0]] = node
            self.by_type.setdefault(row[1], []).append(row[0])
        # Only edges a walk can follow are kept, which drops the structural bulk (table → column, project → file).
        self.out: dict[str, list[tuple[str, str, str]]] = {}
        kinds = sorted(FOLLOW_EDGES | {"CONTAINS"})
        for edge_id, source, target, edge_type in db.execute(
            f"SELECT edge_id,source_node_id,target_node_id,edge_type FROM graph_edges "
            f"WHERE graph_layer<>'DATA_FLOW' AND edge_type IN ({','.join('?' * len(kinds))})", kinds,
        ):
            if source not in self.nodes or target not in self.nodes or not self.follows(source, edge_type):
                continue
            self.out.setdefault(source, []).append((target, edge_type, edge_id))
        self._reach: dict[tuple, list[str]] = {}

    def follows(self, source: str, edge_type: str) -> bool:
        if edge_type == "CONTAINS":
            return self.nodes[source]["node_type"] in CONTAINS_FROM
        return edge_type in FOLLOW_EDGES

    def reach(self, entries: Iterable[str], limit: int) -> list[str]:
        """Breadth-first walk from the entries; data nodes end a path."""
        entries = tuple(entries)
        key = (entries, limit)
        if key not in self._reach:
            self._reach[key] = self._walk(entries, limit)
        return self._reach[key]

    def _walk(self, entries: tuple[str, ...], limit: int) -> list[str]:
        seen: dict[str, None] = {}
        queue = deque(node_id for node_id in entries if node_id in self.nodes)
        while queue and len(seen) < limit:
            node_id = queue.popleft()
            if node_id in seen:
                continue
            seen[node_id] = None
            if self.nodes[node_id]["node_type"] in TERMINAL_TYPES:
                continue
            for target, edge_type, _ in self.out.get(node_id, ()):
                if target not in seen and self.follows(node_id, edge_type):
                    queue.append(target)
        return list(seen)

    def edges_within(self, node_ids: Iterable[str]) -> list[dict[str, str]]:
        members = set(node_ids)
        return [
            {"edge_id": edge_id, "source": source, "target": target, "edge_type": edge_type}
            for source in members
            for target, edge_type, edge_id in self.out.get(source, ())
            if target in members and self.follows(source, edge_type)
        ]

    def children(self, node_id: str, edge_type: str, node_type: str) -> list[str]:
        return [t for t, kind, _ in self.out.get(node_id, ()) if kind == edge_type and self.nodes[t]["node_type"] == node_type]


_INDEX_CACHE: dict[str, tuple[tuple[int, int], GraphIndex]] = {}


def graph_index(db: sqlite3.Connection, web_path: Path) -> GraphIndex:
    stat = web_path.stat()
    key = (stat.st_size, stat.st_mtime_ns)
    cached = _INDEX_CACHE.get(str(web_path))
    if cached and cached[0] == key:
        return cached[1]
    index = GraphIndex(db)
    after = web_path.stat()
    if (after.st_size, after.st_mtime_ns) == key:  # not replaced by an extract while it was read
        _INDEX_CACHE[str(web_path)] = (key, index)
    return index


def graph_hash(web_path: Path) -> str:
    stat = web_path.stat()
    return hashlib.sha1(f"{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:12]


DATABASE_TYPES = frozenset({"TABLE", "VIEW", "MATERIALIZED_VIEW", "COLUMN", "SEQUENCE", "SYNONYM", "PROCEDURE", "FUNCTION",
                            "TRIGGER", "PLSQL_PACKAGE", "DATABASE", "DATABASE_SCHEMA", "INLINE_SQL", "SQL_FILE"})
JOB_TYPES = frozenset({"JOB_NETWORK", "JOB"})


def system_of(node_type: str, system_key: str | None, database_key: str | None, repository_key: str | None, node_id: str) -> str:
    """The system a node runs in, for swimlanes: the database for DB objects, the repository for code."""
    if node_type in JOB_TYPES:
        return "Jobnet"
    if node_type in {"EXTERNAL_DATABASE_OBJECT", "DATABASE_LINK"}:
        return database_key or "Hệ thống ngoài"
    if node_type in {"EXTERNAL_API_OPERATION", "EXTERNAL_SYSTEM"}:
        return system_key or "Hệ thống ngoài"
    if node_type == "UNRESOLVED_REFERENCE":
        return "Chưa xác định"
    if node_type in DATABASE_TYPES and database_key:
        return database_key
    return repository_key or system_key or database_key or "Khác"


def ref_name(node: dict[str, Any]) -> str:
    """The name a document uses for a node: Class.method for code, the technical name otherwise."""
    technical = node["technical_name"]
    if node["node_type"] in {"METHOD", "LOCAL_ROUTINE", "UI_ACTION", "PROCEDURE", "FUNCTION"} and "." not in technical:
        parts = [part for part in re.split(r"[.:]", node["qualified_name"]) if part]
        if len(parts) >= 2 and parts[-1] == technical and not parts[-2].lower() in {"cs", "ts", "sql", "js"}:
            return f"{parts[-2]}.{technical}"
    return technical or node["display_name"]


def ref_of(node: dict[str, Any]) -> str:
    return f"{REF_KEYS.get(node['node_type'], node['node_type'].lower())}: {ref_name(node)}"


def feature_labels(index: GraphIndex, node_ids: Iterable[str]) -> dict[str, tuple[str, str]]:
    """(ref, name) for each node, unique within the set: two nodes with the same name (the same table in two
    databases) are told apart by their system, e.g. `table: DB2.ORDER_HEADER`."""
    ids = [node_id for node_id in dict.fromkeys(node_ids) if node_id in index.nodes]
    counts: dict[str, int] = {}
    for node_id in ids:
        counts[ref_name(index.nodes[node_id])] = counts.get(ref_name(index.nodes[node_id]), 0) + 1
    labels: dict[str, tuple[str, str]] = {}
    taken: set[str] = set()
    for node_id in ids:
        node = index.nodes[node_id]
        name = ref_name(node)
        if counts[name] > 1:
            name = f"{node['system']}.{name}" if node.get("system") else name
            if name in taken:
                name = f"{name}#{node_id.rsplit(':', 1)[-1][:12]}"
        taken.add(name)
        labels[node_id] = (f"{REF_KEYS.get(node['node_type'], node['node_type'].lower())}: {name}", name)
    return labels


# --------------------------------------------------------------------------- tree

def _route_of(node: dict[str, Any]) -> str:
    return str(node["properties"].get("route") or node["technical_name"]).strip("/")


def _menu_key(route: str) -> str:
    for segment in route.split("/"):
        if segment and not segment.startswith(":") and not segment.startswith("{"):
            return segment
    return "root"


def _title(text: str) -> str:
    words = re.sub(r"[-_]+", " ", text).strip()
    return words[:1].upper() + words[1:] if words else text


def _items(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {row["item_id"]: dict(row) for row in db.execute("SELECT * FROM kn.biz_items")}


def _docs(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        row["flow_id"]: {"feature_id": row["feature_id"], "doc": _json(row["doc_json"], {}), "updated_at": row["updated_at"]}
        for row in db.execute("SELECT flow_id,feature_id,doc_json,updated_at FROM kn.biz_flows")
    }


def _parts(doc: dict[str, Any]) -> list[str]:
    if not doc:
        return []
    statuses = [doc.get("summary", {}).get("status", "ai")]
    statuses += [step.get("status", "ai") for step in doc.get("steps", [])]
    if doc.get("edge_cases", {}).get("items"):
        statuses.append(doc["edge_cases"].get("status", "ai"))
    return statuses


def _open_questions(doc: dict[str, Any]) -> int:
    return sum(1 for question in doc.get("questions", []) if not question.get("answer"))


def build_tree(db: sqlite3.Connection, index: GraphIndex) -> dict[str, Any]:
    items, docs = _items(db), _docs(db)
    menus: dict[str, dict[str, Any]] = {}
    features: dict[str, dict[str, Any]] = {}

    def menu(menu_id: str, name: str, source: str) -> dict[str, Any]:
        return menus.setdefault(menu_id, {"id": menu_id, "kind": "menu", "name": name, "source": source, "children": []})

    for screen_id in index.by_type.get("SCREEN", []):
        node = index.nodes[screen_id]
        route = _route_of(node)
        key = _menu_key(route)
        feature = {
            "id": f"feature:{screen_id}", "kind": "feature", "name": _title(node["display_name"] or route), "route": route,
            "entry_node_id": screen_id, "entry_type": "SCREEN", "batch": False, "parent_id": f"menu:{key}",
            "flow_entries": index.children(screen_id, "CONTAINS", "UI_ACTION") or [screen_id],
        }
        features[feature["id"]] = feature
        menu(f"menu:{key}", _title(key), "route")
    for network_id in index.by_type.get("JOB_NETWORK", []):
        node = index.nodes[network_id]
        feature = {
            "id": f"feature:{network_id}", "kind": "feature", "name": node["display_name"], "route": node["technical_name"],
            "entry_node_id": network_id, "entry_type": "JOB_NETWORK", "batch": True, "parent_id": BATCH_MENU,
            "flow_entries": index.children(network_id, "CONTAINS", "JOB") or [network_id],
        }
        features[feature["id"]] = feature
        menu(BATCH_MENU, "Chạy nền", "jobnet")

    # Entry points people attached to a feature (item "entry:<node>" with that parent).
    extra_entries: dict[str, list[str]] = {}
    for item_id, item in items.items():
        if item_id.startswith("entry:") and item.get("parent_id") in features and item_id[6:] in index.nodes:
            extra_entries.setdefault(item["parent_id"], []).append(item_id[6:])

    reached: set[str] = set()
    for feature in features.values():
        feature["extra_entries"] = extra_entries.get(feature["id"], [])
        reached.update(index.reach([feature["entry_node_id"], *feature["extra_entries"]], MAX_FEATURE_NODES))

    # A UI action or job shared by two screens/jobnets is one flow with one owner: the feature its document
    # was saved under, otherwise the first feature by id. The other features do not list it.
    owners: dict[str, str] = {}
    for feature_id in sorted(features):
        for entry in [*features[feature_id]["flow_entries"], *features[feature_id]["extra_entries"]]:
            flow_id = f"flow:{entry}"
            stored = docs.get(flow_id)
            if stored and stored["feature_id"] == feature_id:
                owners[flow_id] = feature_id
            else:
                owners.setdefault(flow_id, feature_id)
    for feature in features.values():
        flows = []
        for entry in dict.fromkeys([*feature["flow_entries"], *feature["extra_entries"]]):
            flow_id = f"flow:{entry}"
            if owners.get(flow_id) != feature["id"]:
                continue
            stored = docs.pop(flow_id, None)
            flows.append(_flow_item(flow_id, (stored or {}).get("doc") or None, index.nodes[entry], feature["id"]))
        feature["children"] = flows

    # Flows imported for a feature under an id the graph no longer proposes; documents whose feature is gone
    # after a re-extract are kept reachable under their own menu so nobody's text is lost.
    stranded = []
    for flow_id, stored in docs.items():
        if stored["feature_id"] in features:
            features[stored["feature_id"]]["children"].append(_flow_item(flow_id, stored["doc"], None, stored["feature_id"]))
        else:
            stranded.append(_flow_item(flow_id, stored["doc"], None, stored["feature_id"]))

    assigned = {node_id for entries in extra_entries.values() for node_id in entries}
    unassigned = sorted(
        (node_id for node_id in index.by_type.get("API_OPERATION", []) if node_id not in reached and node_id not in assigned),
        key=lambda value: index.nodes[value]["technical_name"],
    )
    # A large system can have thousands; the tree lists the first ones, search finds the rest.
    orphans = []
    for node_id in unassigned[:MAX_UNASSIGNED]:
        orphan = {"id": f"entry:{node_id}", "kind": "entry", "name": ref_name(index.nodes[node_id]), "node_id": node_id,
                  "node_type": index.nodes[node_id]["node_type"]}
        _apply_item(orphan, items.get(orphan["id"]))
        orphans.append(orphan)

    for feature in features.values():
        _apply_item(feature, items.get(feature["id"]))
        parent = feature["parent_id"]
        if parent not in menus:
            menu(parent, _title(parent.split(":", 1)[-1]), "user")
        menus[parent]["children"].append(feature)
    for menu_item in menus.values():
        _apply_item(menu_item, items.get(menu_item["id"]))
        menu_item["children"].sort(key=lambda child: (child.get("sort_order") or 0, child["name"].lower()))
    # Ordered by the route key, so renaming a menu does not move it.
    ordered = sorted(menus.values(), key=lambda m: (m.get("sort_order") or 0, m["id"] == BATCH_MENU, m["id"]))
    if stranded:
        ordered.append({"id": STRANDED_MENU, "kind": "menu", "name": "Tài liệu không còn tính năng", "source": "graph",
                        "stranded": True, "children": stranded})
    ordered.append({"id": UNSORTED_MENU, "kind": "menu", "name": "Chưa xếp vào tính năng", "source": "graph",
                    "unsorted": True, "children": orphans, "total": len(unassigned)})
    for item in ordered:
        _summarize(item)
    return {"menus": ordered, "graph_nodes": len(index.nodes)}


def _flow_item(flow_id: str, doc: dict[str, Any] | None, entry: dict[str, Any] | None, feature_id: str) -> dict[str, Any]:
    name = (doc or {}).get("name") or (entry["display_name"] if entry else flow_id)
    return {
        "id": flow_id, "kind": "flow", "name": name, "feature_id": feature_id, "has_doc": bool(doc),
        "entry_node_id": entry["node_id"] if entry else None, "parts": _parts(doc or {}),
        "questions": _open_questions(doc or {}), "summary": ((doc or {}).get("summary") or {}).get("text", ""),
    }


def _apply_item(target: dict[str, Any], item: dict[str, Any] | None) -> None:
    target.setdefault("description", "")
    target.setdefault("status", None)
    if not item:
        return
    for key in ("name", "description", "status", "reviewed_by", "sort_order"):
        if item.get(key) not in (None, ""):
            target[key] = item[key]
    if item.get("parent_id") and target["kind"] == "feature":
        target["parent_id"] = item["parent_id"]


def _summarize(item: dict[str, Any]) -> None:
    flows = list(_flows_under(item))
    item["flow_count"] = len(flows)
    item["parts"] = [status for flow in flows for status in (flow["parts"] or ["none"])]
    item["questions"] = sum(flow["questions"] for flow in flows)
    for child in item.get("children", []):
        if child["kind"] in {"menu", "feature"}:
            _summarize(child)


def _flows_under(item: dict[str, Any]):
    if item["kind"] == "flow":
        yield item
    for child in item.get("children", []):
        yield from _flows_under(child)


def find_item(tree: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    stack = list(tree["menus"])
    while stack:
        item = stack.pop()
        if item["id"] == item_id:
            return item
        stack.extend(item.get("children", []))
    return None


# --------------------------------------------------------------------------- node facts

def node_facts(db: sqlite3.Connection, index: GraphIndex, node_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Display facts for nodes: document name, file and line, localized names, business term."""
    facts = {
        node_id: {
            "node_id": node_id, "node_type": index.nodes[node_id]["node_type"], "name": ref_name(index.nodes[node_id]),
            "ref": ref_of(index.nodes[node_id]), "technical_name": index.nodes[node_id]["technical_name"],
            "display_name": index.nodes[node_id]["display_name"], "system": index.nodes[node_id]["system"],
            "file": "", "line": None, "names": {}, "term": "",
        }
        for node_id in node_ids if node_id in index.nodes
    }
    ids = list(facts)
    for chunk in _chunks(ids, 400):
        marks = ",".join("?" * len(chunk))
        for row in db.execute(
            f"SELECT target_id,source_path,start_line FROM graph_evidence WHERE target_type='NODE' AND target_id IN ({marks}) "
            "ORDER BY evidence_id", chunk,
        ):
            fact = facts[row[0]]
            if not fact["file"]:
                fact["file"], fact["line"] = row[1], row[2]
        for row in db.execute(
            f"SELECT target_id,locale,value FROM graph_localization WHERE field_name='name' AND target_id IN ({marks}) "
            "AND UPPER(review_status)<>'REJECTED'", chunk,
        ):
            facts[row[0]]["names"][row[1]] = row[2]
        for row in db.execute(
            f"""SELECT m.target_id, COALESCE(vi.label, en.label, '') FROM graph_business_semantic_mappings m
                LEFT JOIN graph_business_semantics vi ON vi.semantic_id=m.semantic_id AND vi.locale='vi'
                LEFT JOIN graph_business_semantics en ON en.semantic_id=m.semantic_id AND en.locale='en'
                WHERE m.status='approved' AND m.target_id IN ({marks})""", chunk,
        ):
            facts[row[0]]["term"] = facts[row[0]]["term"] or row[1]
    return facts


def _chunks(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


# --------------------------------------------------------------------------- flows

def skeleton(index: GraphIndex, flow_id: str, entry: str | None, name: str = "") -> dict[str, Any]:
    """A flow document with one step per system the flow crosses, every text left for AI.

    The walk from the entry is breadth-first, so systems come in the order the flow reaches them
    (customer-web → order-api → ORDER_DB → …); each step then sits in one swimlane.
    """
    empty = {"id": flow_id, "name": name, "summary": {"text": "", "status": "ai"}, "facts": [], "steps": [],
             "edge_cases": {"items": [], "status": "ai"}, "questions": []}
    if not entry or entry not in index.nodes:
        return empty
    by_system: dict[str, list[str]] = {}
    for node_id in index.reach([entry], MAX_FLOW_NODES):
        by_system.setdefault(index.nodes[node_id]["system"], []).append(node_id)
    empty["steps"] = [
        {"id": f"s{position}", "system": system, "name": "", "actor": "", "text": "", "status": "ai", "nodes": members, "rules": []}
        for position, (system, members) in enumerate(by_system.items(), start=1)
    ]
    return empty


def flow_entry(flow_id: str) -> str | None:
    return flow_id[5:] if flow_id.startswith("flow:") else None


def load_flow(db: sqlite3.Connection, index: GraphIndex, tree: dict[str, Any], flow_id: str) -> dict[str, Any]:
    item = find_item(tree, flow_id)
    if not item or item["kind"] != "flow":
        raise BusinessError("not_found", "Flow not found", 404)
    row = db.execute("SELECT doc_json FROM kn.biz_flows WHERE flow_id=?", (flow_id,)).fetchone()
    doc = _json(row[0], {}) if row else skeleton(index, flow_id, item["entry_node_id"], item["name"])
    node_ids = list(dict.fromkeys(node_id for step in doc.get("steps", []) for node_id in step.get("nodes", [])))
    if item["entry_node_id"] and item["entry_node_id"] in index.nodes:
        node_ids = list(dict.fromkeys([*node_ids, *index.reach([item["entry_node_id"]], MAX_FLOW_NODES)]))
    node_ids = [node_id for node_id in node_ids if node_id in index.nodes]
    feature = find_item(tree, item["feature_id"])
    return {
        "flow": {**doc, "id": flow_id, "name": doc.get("name") or item["name"]},
        "has_doc": bool(row), "feature_id": item["feature_id"], "feature_name": feature["name"] if feature else "",
        "path": _path(tree, flow_id), "nodes": list(node_facts(db, index, node_ids).values()),
        "edges": index.edges_within(node_ids), "entry_node_id": item["entry_node_id"],
        "levels": semantic_levels(db, node_ids, item["entry_node_id"]),
    }


def semantic_levels(db: sqlite3.Connection, node_ids: list[str], entry: str | None) -> dict[str, dict[str, Any]]:
    """The flow graph projected like the explorer's System/Application levels (Technical is the flow itself):
    only MAIN nodes stay, Application drops structural edges and System keeps data-flow edges only."""
    members = set(node_ids)
    roles: dict[str, str] = {}
    rows: list[tuple] = []
    for start in range(0, len(node_ids), 500):
        chunk = node_ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        roles.update(db.execute(f"SELECT node_id,graph_role FROM graph_nodes WHERE node_id IN ({marks})", chunk).fetchall())
        rows += [row for row in db.execute(
            f"SELECT edge_id,source_node_id,target_node_id,edge_type,graph_layer FROM graph_edges WHERE source_node_id IN ({marks}) ORDER BY edge_id",
            chunk) if row[2] in members]
    main = {node_id for node_id in node_ids if roles.get(node_id) == "MAIN"} | ({entry} & members if entry else set())
    visible = {"SYSTEM": lambda layer: layer == "DATA_FLOW", "APPLICATION": lambda layer: layer != "STRUCTURAL"}
    return {level: {
        "nodes": [node_id for node_id in node_ids if node_id in main],
        "edges": [{"edge_id": e, "source": s, "target": t, "edge_type": k} for e, s, t, k, layer in rows
                  if s in main and t in main and s != t and test(layer)],
    } for level, test in visible.items()}


def _path(tree: dict[str, Any], item_id: str) -> list[dict[str, str]]:
    def walk(items, trail):
        for item in items:
            here = [*trail, {"id": item["id"], "name": item["name"], "kind": item["kind"]}]
            if item["id"] == item_id:
                return here
            found = walk(item.get("children", []), here)
            if found:
                return found
        return None
    return walk(tree["menus"], []) or []


def feature_detail(db: sqlite3.Connection, index: GraphIndex, tree: dict[str, Any], feature_id: str) -> dict[str, Any]:
    item = find_item(tree, feature_id)
    if not item or item["kind"] not in {"feature", "menu"}:
        raise BusinessError("not_found", "Feature not found", 404)
    result = {**item, "path": _path(tree, feature_id)}
    if item["kind"] == "feature":
        node_ids = index.reach([item["entry_node_id"], *item.get("extra_entries", [])], MAX_FEATURE_NODES)
        writes, reads = set(), set()
        for source in node_ids:
            for target, edge_type, _ in index.out.get(source, ()):
                if edge_type in WRITE_EDGES:
                    writes.add(target)
                elif edge_type in READ_EDGES:
                    reads.add(target)
        name = lambda node_id: ref_name(index.nodes[node_id])  # noqa: E731
        result.update({
            "entry": {"node_id": item["entry_node_id"], "name": name(item["entry_node_id"]), "node_type": item["entry_type"]},
            "apis": sorted({name(n) for n in node_ids if index.nodes[n]["node_type"] == "API_OPERATION"}),
            "writes": sorted(name(n) for n in writes if n in index.nodes),
            "reads": sorted(name(n) for n in reads - writes if n in index.nodes),
            "node_count": len(node_ids),
            "last_export": _last_export(db, feature_id),
        })
    return result


def _last_export(db: sqlite3.Connection, feature_id: str) -> dict[str, str] | None:
    row = db.execute(
        "SELECT export_id,created_at FROM kn.biz_exports WHERE feature_id=? ORDER BY created_at DESC, export_id DESC LIMIT 1",
        (feature_id,),
    ).fetchone()
    return {"export_id": row[0], "created_at": row[1]} if row else None


# --------------------------------------------------------------------------- edits

def _text(value: Any, limit: int, field: str) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise BusinessError("invalid_business", f"{field} must be text up to {limit} characters")
    return value.strip()


def update_item(db: sqlite3.Connection, tree: dict[str, Any], item_id: str, body: dict[str, Any], who: str,
                index: GraphIndex | None = None) -> None:
    item = find_item(tree, item_id)
    if not item and index is not None and item_id.startswith("entry:") and item_id[6:] in index.nodes:
        # Entry points past the tree's first page (or currently attached to a feature) are still assignable.
        item = {"id": item_id, "kind": "entry"}
    if not item or item["kind"] not in {"menu", "feature", "entry"} or item_id in {UNSORTED_MENU, STRANDED_MENU}:
        raise BusinessError("not_found", "Menu, feature or entry point not found", 404)
    allowed = {"name", "description", "status", "parent_id"}
    if not body or set(body) - allowed:
        raise BusinessError("invalid_business", "Only name, description, status and parent_id can change")
    current = db.execute("SELECT * FROM kn.biz_items WHERE item_id=?", (item_id,)).fetchone()
    row = dict(current) if current else {"item_id": item_id}
    if "name" in body:
        row["name"] = _text(body["name"], 200, "name") or None
    if "description" in body:
        row["description"] = _text(body["description"], 20_000, "description")
    if "parent_id" in body:
        parent = find_item(tree, body["parent_id"]) if isinstance(body["parent_id"], str) else None
        wanted = "feature" if item["kind"] == "entry" else "menu"
        if item["kind"] == "menu" or not parent or parent["kind"] != wanted or parent["id"] in {UNSORTED_MENU, STRANDED_MENU}:
            raise BusinessError("invalid_business", f"parent_id must be a {wanted}")
        row["parent_id"] = parent["id"]
    if "status" in body:
        if body["status"] != "approved":
            raise BusinessError("invalid_business", "status can only be set to approved")
        row["status"], row["reviewed_by"] = "approved", who
    elif {"name", "description"} & set(body):
        row["status"], row["reviewed_by"] = "edited", who
    db.execute(
        """INSERT INTO kn.biz_items(item_id,parent_id,name,description,status,reviewed_by,sort_order,updated_at)
           VALUES(:item_id,:parent_id,:name,:description,:status,:reviewed_by,:sort_order,:updated_at)
           ON CONFLICT(item_id) DO UPDATE SET parent_id=excluded.parent_id,name=excluded.name,description=excluded.description,
             status=excluded.status,reviewed_by=excluded.reviewed_by,updated_at=excluded.updated_at""",
        {"parent_id": None, "name": None, "description": None, "status": None, "reviewed_by": None, "sort_order": None,
         **row, "updated_at": _now()},
    )
    db.commit()


def _save_flow(db: sqlite3.Connection, flow_id: str, feature_id: str, doc: dict[str, Any]) -> None:
    db.execute(
        """INSERT INTO kn.biz_flows(flow_id,feature_id,doc_json,updated_at) VALUES(?,?,?,?)
           ON CONFLICT(flow_id) DO UPDATE SET feature_id=excluded.feature_id,doc_json=excluded.doc_json,updated_at=excluded.updated_at""",
        (flow_id, feature_id, json.dumps(doc, ensure_ascii=False), _now()),
    )


def _begin(db: sqlite3.Connection) -> None:
    """Read-modify-write of a flow document under a write lock, so two edits (or an edit and an import)
    cannot overwrite each other."""
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN IMMEDIATE")


def _stored_or_skeleton(db: sqlite3.Connection, index: GraphIndex, item: dict[str, Any]) -> dict[str, Any]:
    row = db.execute("SELECT doc_json FROM kn.biz_flows WHERE flow_id=?", (item["id"],)).fetchone()
    return _json(row[0], {}) if row else skeleton(index, item["id"], item["entry_node_id"], item["name"])


def update_part(db: sqlite3.Connection, index: GraphIndex, tree: dict[str, Any], flow_id: str, part: str,
                body: dict[str, Any], who: str) -> dict[str, Any]:
    item = find_item(tree, flow_id)
    if not item or item["kind"] != "flow":
        raise BusinessError("not_found", "Flow not found", 404)
    _begin(db)
    doc = _stored_or_skeleton(db, index, item)
    if part == "summary":
        target, fields = doc.setdefault("summary", {"text": "", "status": "ai"}), {"text": 50_000, "name": 200}
    elif part == "edge_cases":
        target, fields = doc.setdefault("edge_cases", {"items": [], "status": "ai"}), {"items": None}
    elif part.startswith("step:"):
        step_id = part[5:]
        target = next((step for step in doc.get("steps", []) if step.get("id") == step_id), None)
        if target is None:
            raise BusinessError("not_found", "Step not found", 404)
        fields = {"name": 200, "actor": 200, "text": 50_000, "nodes": None, "rules": None}
    else:
        raise BusinessError("invalid_business", "part must be summary, edge_cases or step:<id>")
    if not body or set(body) - {*fields, "status"}:
        raise BusinessError("invalid_business", f"Only {', '.join(sorted(fields))} or status can change")
    for key, limit in fields.items():
        if key not in body:
            continue
        if key == "name" and part == "summary":
            doc["name"] = _text(body[key], limit, key)
        elif key == "items":
            values = body[key]
            if not isinstance(values, list) or len(values) > 100:
                raise BusinessError("invalid_business", "items must be a list of up to 100 texts")
            target["items"] = [_text(value, 4000, "item") for value in values if isinstance(value, str) and value.strip()]
        elif key == "nodes":
            values = body[key]
            if not isinstance(values, list) or any(not isinstance(value, str) or value not in index.nodes for value in values):
                raise BusinessError("invalid_business", "nodes must be existing node ids")
            target["nodes"] = list(dict.fromkeys(values))
        elif key == "rules":
            target["rules"] = _clean_rules(body[key], target.get("rules", []))
        else:
            target[key] = _text(body[key], limit, key)
    if "status" in body:
        if body["status"] != "approved":
            raise BusinessError("invalid_business", "status can only be set to approved")
        target["status"], target["reviewed_by"] = "approved", who
        # Behaviour fingerprint of the nodes it describes: a later extract that changes them puts the part
        # back in the review queue (web/backend/knowledge.py).
        from web.backend.knowledge import fingerprint_of

        nodes = target.get("nodes") if part.startswith("step:") else [
            node for step in doc.get("steps", []) for node in step.get("nodes", [])
        ]
        target["fingerprint"] = fingerprint_of(db, nodes or [])
    else:
        target["status"], target["reviewed_by"] = "edited", who
        target.pop("fingerprint", None)
    _save_flow(db, flow_id, item["feature_id"], doc)
    db.commit()
    return doc


def _clean_rules(values: Any, previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > 50:
        raise BusinessError("invalid_business", "rules must be a list of up to 50 rules")
    rules = []
    for position, value in enumerate(values):
        if not isinstance(value, dict):
            raise BusinessError("invalid_business", "each rule must be an object")
        # A rule keeps an omitted field only when the client sent the same rule (same title) back.
        old = previous[position] if position < len(previous) and previous[position].get("title") == value.get("title") else {}
        rules.append({
            "title": _text(value.get("title", old.get("title", "")), 400, "rule title"),
            "text": _text(value.get("text", old.get("text", "")), 8000, "rule text"),
            "at": _text(value.get("at", old.get("at", "")), 400, "rule at"),
            "evidence": _text(value.get("evidence", old.get("evidence", "")), 400, "rule evidence"),
            "status": value.get("status", old.get("status", "warn")) if value.get("status", old.get("status", "warn")) in RULE_STATUSES else "warn",
        })
    return rules


def answer_question(db: sqlite3.Connection, index: GraphIndex, tree: dict[str, Any], flow_id: str, question_id: str,
                    answer: Any, who: str) -> dict[str, Any]:
    item = find_item(tree, flow_id)
    if not item or item["kind"] != "flow":
        raise BusinessError("not_found", "Flow not found", 404)
    _begin(db)
    doc = _stored_or_skeleton(db, index, item)
    question = next((q for q in doc.get("questions", []) if q.get("id") == question_id), None)
    if question is None:
        raise BusinessError("not_found", "Question not found", 404)
    question["answer"], question["answered_by"] = _text(answer, 8000, "answer"), who
    if not question["answer"]:
        raise BusinessError("invalid_business", "answer must not be empty")
    _save_flow(db, flow_id, item["feature_id"], doc)
    db.commit()
    return doc


# --------------------------------------------------------------------------- AI export / import

def _graph_sqlite(web_path: Path) -> Path:
    return web_path.parent / "graph.sqlite"


def _engine_extras(web_path: Path, node_ids: list[str]) -> tuple[dict[str, list[dict]], dict[str, list[str]]]:
    """Conditions from semantic trees and source comments, which only graph.sqlite has."""
    conditions: dict[str, list[dict]] = {}
    comments: dict[str, list[str]] = {}
    path = _graph_sqlite(web_path)
    if not path.is_file() or not node_ids:
        return conditions, comments
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as graph:
            for chunk in _chunks(node_ids, 400):
                marks = ",".join("?" * len(chunk))
                for stable_id, properties in graph.execute(
                    f"SELECT stable_id,properties_json FROM nodes WHERE stable_id IN ({marks})", chunk
                ):
                    found = _conditions(_json(properties, {}).get("semantic_tree"))
                    if found:
                        conditions[stable_id] = found
                for stable_id, text in graph.execute(
                    f"""SELECT n.stable_id,c.normalized_text FROM comments c JOIN nodes n ON n.node_id=c.owner_node_id
                        WHERE n.stable_id IN ({marks}) ORDER BY c.start_line""", chunk,
                ):
                    if text and text.strip():
                        comments.setdefault(stable_id, []).append(text.strip()[:400])
    except sqlite3.Error:
        return {}, {}
    return conditions, comments


def _conditions(tree: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if len(found) >= MAX_CONDITIONS_PER_NODE:
            return
        if isinstance(value, dict):
            kind = value.get("type")
            if kind in CONDITION_KINDS:
                text = value.get("condition") or value.get("label") or value.get("text") or ""
                if isinstance(text, (dict, list)):
                    text = json.dumps(text, ensure_ascii=False)[:200]
                # The extractor writes `source: {path, line}`; older trees used `line`/`location`.
                source = value.get("source") if isinstance(value.get("source"), dict) else {}
                location = value.get("location") if isinstance(value.get("location"), dict) else {}
                line = value.get("line") or value.get("start_line") or source.get("line") or location.get("line")
                found.append({"kind": kind, "text": str(text)[:300], **({"line": line} if line else {})})
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(tree)
    return found


def export_pack(db: sqlite3.Connection, index: GraphIndex, tree: dict[str, Any], web_path: Path, feature_id: str,
                *, include_code: bool = True, include_locked: bool = True, include_answers: bool = True,
                include_instructions: bool = True) -> dict[str, Any]:
    feature = find_item(tree, feature_id)
    if not feature or feature["kind"] != "feature":
        raise BusinessError("not_found", "Feature not found", 404)
    node_ids = index.reach([feature["entry_node_id"], *feature.get("extra_entries", [])], MAX_FEATURE_NODES)
    facts = node_facts(db, index, node_ids)
    labels = feature_labels(index, node_ids)
    for node_id, (ref, name) in labels.items():
        facts[node_id]["ref"], facts[node_id]["name"] = ref, name
    edges = index.edges_within(node_ids)
    evidence: dict[str, dict[str, str]] = {}
    if include_code:
        for chunk in _chunks(node_ids, 400):
            marks = ",".join("?" * len(chunk))
            for target, source_path, line, snippet in db.execute(
                f"""SELECT target_id,source_path,start_line,snippet FROM graph_evidence
                    WHERE target_type='NODE' AND target_id IN ({marks}) AND snippet<>'' ORDER BY evidence_id""", chunk,
            ):
                name = facts[target]["name"]
                if name not in evidence:
                    evidence[name] = {"at": f"{source_path}:{line}" if line else source_path, "code": snippet[:MAX_EVIDENCE_CHARS]}
    conditions, comments = _engine_extras(web_path, node_ids) if include_code else ({}, {})
    nodes = []
    for node_id in node_ids:
        fact = facts[node_id]
        entry = {"ref": fact["ref"]}
        if fact["file"]:
            entry["file"] = f"{fact['file']}:{fact['line']}" if fact["line"] else fact["file"]
        if fact["term"]:
            entry["term"] = fact["term"]
        for locale in ("ja", "en"):
            if fact["names"].get(locale):
                entry[locale] = fact["names"][locale]
        if node_id in conditions:
            entry["conditions"] = conditions[node_id]
        if node_id in comments:
            entry["comments"] = comments[node_id][:5]
        nodes.append(entry)

    flows, locked, answered = [], 0, []
    for item in feature["children"]:
        doc = _stored_or_skeleton(db, index, item)
        exported, count = _export_flow(index, doc, item, include_locked, labels)
        locked += count
        flows.append(exported)
        if include_answers:
            answered += [{"q": q["text"], "a": q["answer"]} for q in doc.get("questions", []) if q.get("answer")]

    count = db.execute("SELECT COUNT(*) FROM kn.biz_exports").fetchone()[0]
    export_id = f"exp-{count + 1:04d}-{uuid.uuid4().hex[:8]}"
    current_hash = graph_hash(web_path)
    db.execute("INSERT INTO kn.biz_exports(export_id,feature_id,graph_hash,created_at) VALUES(?,?,?,?)",
               (export_id, feature_id, current_hash, _now()))
    db.commit()
    path = " / ".join(step["name"] for step in _path(tree, feature_id))
    pack = {
        "export": {"id": export_id, "feature_id": feature_id, "feature": path, "graph_hash": current_hash, "generated": _now()},
        "context": {
            "feature": {"name": feature["name"], "route": feature["route"], "entry": labels[feature["entry_node_id"]][0],
                        "description": feature.get("description", "")},
            "nodes": nodes,
            "edges": [[facts[e["source"]]["name"], e["edge_type"], facts[e["target"]]["name"]] for e in edges],
            **({"evidence": evidence} if include_code else {}),
            **({"answered": answered} if answered else {}),
        },
        "flows": flows,
    }
    slug = re.sub(r"[^a-z0-9]+", "-", feature["route"].lower()).strip("-") or "feature"
    dump = lambda value: yaml.dump(value, Dumper=_BlockDumper, allow_unicode=True, sort_keys=False, width=100)
    if include_instructions:
        # One instruction file: the guide, the data, and the YAML block the AI fills in and sends back.
        text = (f'# Hướng dẫn cho AI · viết tài liệu nghiệp vụ cho tính năng "{path}"\n\n{INSTRUCTIONS}\n'
                "# Dữ liệu\n\nChỉ để đọc, không trả về phần này.\n\n"
                f"```yaml\n{dump({'context': pack['context']})}```\n\n"
                "# Định dạng trả về\n\nĐiền các trường trống trong khối dưới đây và trả về đúng một khối YAML này.\n\n"
                f"```yaml\n{dump({'export': pack['export'], 'flows': pack['flows']})}```\n")
        filename = f"{slug}.codemap-ai.md"
    else:
        text = (f'# Code Map · tính năng "{feature["name"]}". `context` là dữ liệu; `flows` là định dạng trả về.\n'
                + dump(pack))
        filename = f"{slug}.codemap-ai.yaml"
    return {"export_id": export_id, "filename": filename, "text": text,
            "stats": {"nodes": len(nodes), "edges": len(edges), "evidence": len(evidence), "locked": locked, "flows": len(flows)}}


def _export_flow(index: GraphIndex, doc: dict[str, Any], item: dict[str, Any], include_locked: bool,
                 labels: dict[str, tuple[str, str]]) -> tuple[dict, int]:
    locked = 0

    def part(source: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        nonlocal locked
        if include_locked and source.get("status") in {"edited", "approved"}:
            locked += 1
            return {"locked": True, **body}
        return {key: [] if isinstance(value, list) else "" for key, value in body.items()}

    def names(node_ids: list[str]) -> list[str]:
        return [labels[node_id][0] if node_id in labels else ref_of(index.nodes[node_id]) for node_id in node_ids if node_id in index.nodes]

    summary = doc.get("summary") or {}
    steps = []
    for step in doc.get("steps", []):
        steps.append({"id": step["id"], "nodes": names(step.get("nodes", [])), **part(step, {
            "name": step.get("name", ""), "actor": step.get("actor", ""), "text": step.get("text", ""),
            "rules": [{key: rule.get(key, "") for key in ("title", "at", "evidence", "status", "text")} for rule in step.get("rules", [])],
        })})
    edge_cases = doc.get("edge_cases") or {}
    return {
        "id": item["id"], "name": doc.get("name") or ("" if not item["has_doc"] else item["name"]),
        "entry": names([item["entry_node_id"]])[0] if item.get("entry_node_id") in index.nodes else "",
        "summary": part(summary, {"text": summary.get("text", "")}),
        "steps": steps,
        "edge_cases": part(edge_cases, {"items": edge_cases.get("items", [])}) if edge_cases.get("items") else {"items": []},
        "questions": [],
    }, locked


_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _load_yaml(text: str) -> Any:
    """safe_load without anchors/aliases: an alias chain is a few bytes that expand to gigabytes once stored."""
    for event in yaml.parse(text, Loader=_YAML_LOADER):
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise BusinessError("invalid_import", "File dùng anchor/alias YAML (&, *). Hãy dán YAML thường.")
    return yaml.load(text, Loader=_YAML_LOADER)


def _parse_pack(text: Any) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise BusinessError("invalid_import", "Dán nội dung file AI trả về")
    text = text.replace("\r\n", "\n")
    for block in re.findall(r"```(?:ya?ml)?[ \t]*\n(.*?)```", text, flags=re.S):
        try:
            doc = _load_yaml(block)
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("flows"), list):
            return doc
    try:
        doc = _load_yaml(text)
    except yaml.YAMLError as exc:
        first = str(exc).splitlines()[0] if str(exc) else "YAML error"
        raise BusinessError("invalid_import", f"YAML sai cú pháp: {first}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("flows"), list):
        raise BusinessError("invalid_import", "Không thấy phần flows. Hãy dán toàn bộ file AI trả về.")
    return doc


def _str(value: Any, limit: int) -> str:
    """Text the AI wrote; numbers are fine, anything structured is dropped rather than stringified."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    return value.strip()[:limit] if isinstance(value, str) else ""


def _strs(value: Any, limit: int, most: int) -> list[str]:
    """A list of texts; a single text where a list was expected counts as a one-item list."""
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [text for text in (_str(item, limit) for item in values[:most]) if text]


def _resolver(index: GraphIndex, node_ids: list[str]):
    by_ref: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for node_id, (ref, name) in feature_labels(index, node_ids).items():
        node = index.nodes[node_id]
        for key in {ref, ref_of(node)}:
            by_ref.setdefault(key, []).append(node_id)
        for key in {name, ref_name(node)}:
            by_name.setdefault(key, []).append(node_id)

    def resolve(value: Any) -> str | None:
        if isinstance(value, dict) and len(value) == 1:
            key, name = next(iter(value.items()))
            value = f"{key}: {name}"
        text = _str(value, 1000)
        matches = by_ref.get(text, [])
        if len(set(matches)) == 1:
            return matches[0]
        name = text.split(":", 1)[1].strip() if re.match(r"^[a-z_]+:\s", text) else text
        matches = by_name.get(name, [])
        return matches[0] if len(set(matches)) == 1 else None

    return resolve


def check_import(db: sqlite3.Connection, index: GraphIndex, tree: dict[str, Any], web_path: Path, feature_id: str,
                 text: Any) -> dict[str, Any]:
    feature = find_item(tree, feature_id)
    if not feature or feature["kind"] != "feature":
        raise BusinessError("not_found", "Feature not found", 404)
    return _check(db, index, feature, web_path, _parse_pack(text))


def _check(db: sqlite3.Connection, index: GraphIndex, feature: dict[str, Any], web_path: Path, pack: dict[str, Any]) -> dict[str, Any]:
    feature_id = feature["id"]
    lines: list[dict[str, str]] = []
    blocking = False
    export = pack.get("export") if isinstance(pack.get("export"), dict) else {}
    row = db.execute("SELECT feature_id,graph_hash FROM kn.biz_exports WHERE export_id=?", (str(export.get("id", "")),)).fetchone()
    if not export.get("id"):
        lines.append({"level": "error", "message": "Thiếu export.id, không biết file thuộc lần xuất nào"}); blocking = True
    elif not row:
        lines.append({"level": "error", "message": f"Không có lần xuất {export['id']} trong Code Map"}); blocking = True
    elif row[0] != feature_id:
        lines.append({"level": "error", "message": f"File thuộc lần xuất {export['id']} của tính năng khác"}); blocking = True
    else:
        lines.append({"level": "ok", "message": f"Đúng tính năng · lần xuất {export['id']}"})
        if row[1] == graph_hash(web_path):
            lines.append({"level": "ok", "message": "Graph chưa đổi kể từ lúc xuất"})
        else:
            # Steps of a flow nobody saved yet are numbered from the current graph, so after a re-extract the
            # ids in the file may point at different steps. Those flows need a fresh export.
            unsaved = [item["name"] for item in feature["children"] if not item["has_doc"]
                       and any(isinstance(flow, dict) and flow.get("id") == item["id"] for flow in pack["flows"])]
            if unsaved:
                lines.append({"level": "error", "message": "Graph đã đổi kể từ lúc xuất và các luồng sau chưa từng được lưu, "
                              f"các bước có thể đã lệch: {', '.join(unsaved)}. Hãy xuất lại."}); blocking = True
            else:
                lines.append({"level": "warn", "message": "Graph đã đổi kể từ lúc xuất. Nên xuất lại để AI viết trên code mới."})

    node_ids = index.reach([feature["entry_node_id"], *feature.get("extra_entries", [])], MAX_FEATURE_NODES)
    resolve = _resolver(index, node_ids)
    known_flows = {item["id"] for item in feature["children"]}
    filled = kept = questions = 0
    bad: dict[str, None] = {}
    for flow in pack["flows"]:
        if not isinstance(flow, dict):
            continue
        if not isinstance(flow.get("id"), str) or flow["id"] not in known_flows:
            lines.append({"level": "warn", "message": f"Luồng {flow.get('id')!r} không thuộc tính năng này, sẽ bỏ qua"})
            continue
        questions += len(_strs(flow.get("questions"), 2000, 50))
        edge = flow.get("edge_cases") if isinstance(flow.get("edge_cases"), dict) else {}
        if not edge.get("locked") and _strs(edge.get("items"), 4000, 100):
            filled += 1
        steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
        for part in [flow.get("summary"), *steps]:
            if not isinstance(part, dict):
                continue
            if part.get("locked"):
                kept += 1
                continue
            rules = [rule for rule in part.get("rules") or [] if isinstance(rule, dict)] if isinstance(part.get("rules"), list) else []
            if _str(part.get("text"), 50_000) or _str(part.get("name"), 200) or rules:
                filled += 1
            for name in re.findall(r"\[\[(.+?)\]\]", _str(part.get("text"), 50_000)):
                if resolve(name) is None:
                    bad[name] = None
            for rule in rules:
                if rule.get("at") and resolve(rule["at"]) is None:
                    bad[_str(rule["at"], 400)] = None
                if not _str(rule.get("evidence"), 400):
                    lines.append({"level": "warn", "message": f"Quy tắc “{_str(rule.get('title'), 400)}” không có evidence, sẽ được đánh dấu “Cần xác nhận”"})
    lines.append({"level": "ok", "message": f"{filled} phần được AI điền"})
    if kept:
        lines.append({"level": "ok", "message": f"{kept} phần khoá được giữ nguyên"})
    if bad:
        lines.append({"level": "error", "message": f"{len(bad)} tham chiếu không có trong graph của tính năng: {', '.join(bad)}. Sẽ hiện là tham chiếu hỏng để bạn sửa."})
    if questions:
        lines.append({"level": "ok", "message": f"{questions} câu hỏi mới"})
    return {"ok": not blocking and filled + questions > 0, "lines": lines}


def apply_import(db: sqlite3.Connection, index: GraphIndex, tree: dict[str, Any], web_path: Path, feature_id: str,
                 text: Any) -> dict[str, Any]:
    feature = find_item(tree, feature_id)
    if not feature or feature["kind"] != "feature":
        raise BusinessError("not_found", "Feature not found", 404)
    pack = _parse_pack(text)
    result = _check(db, index, feature, web_path, pack)
    if not result["ok"]:
        reasons = "; ".join(line["message"] for line in result["lines"] if line["level"] == "error")
        raise BusinessError("invalid_import", "File chưa nhập được: " + (reasons or "không có nội dung mới"))
    node_ids = index.reach([feature["entry_node_id"], *feature.get("extra_entries", [])], MAX_FEATURE_NODES)
    resolve = _resolver(index, node_ids)
    items = {item["id"]: item for item in feature["children"]}
    applied_parts = 0
    applied_flows = []
    locked = {"edited", "approved"}
    _begin(db)
    for flow in pack["flows"]:
        if not isinstance(flow, dict) or not isinstance(flow.get("id"), str) or flow["id"] not in items:
            continue
        item = items[flow["id"]]
        doc = _stored_or_skeleton(db, index, item)
        changed = 0
        summary_now = doc.setdefault("summary", {"text": "", "status": "ai"})
        if _str(flow.get("name"), 200) and summary_now.get("status") not in locked:
            doc["name"] = _str(flow.get("name"), 200)
        summary = flow.get("summary") if isinstance(flow.get("summary"), dict) else {}
        if _str(summary.get("text"), 50_000) and not summary.get("locked") and summary_now.get("status") not in locked:
            doc["summary"] = {"text": _str(summary.get("text"), 50_000), "status": "ai"}
            changed += 1
        incoming = {step["id"]: step for step in (flow.get("steps") if isinstance(flow.get("steps"), list) else [])
                    if isinstance(step, dict) and isinstance(step.get("id"), str)}
        for step in doc.get("steps", []):
            new = incoming.get(step["id"])
            if not new or new.get("locked") or step.get("status") in locked:
                continue
            rules = [rule for rule in new.get("rules") or [] if isinstance(rule, dict)] if isinstance(new.get("rules"), list) else []
            if not (_str(new.get("name"), 200) or _str(new.get("text"), 50_000) or rules):
                continue
            # The step's nodes come from the graph (or a person); the AI only writes about them.
            step.update({
                "name": _str(new.get("name"), 200) or step.get("name", ""),
                "actor": _str(new.get("actor"), 200) or step.get("actor", ""),
                "text": _str(new.get("text"), 50_000),
                "status": "ai",
                "rules": [_import_rule(rule, resolve) for rule in rules[:50]],
            })
            changed += 1
        edge = flow.get("edge_cases") if isinstance(flow.get("edge_cases"), dict) else {}
        edge_items = _strs(edge.get("items"), 4000, 100)
        if edge_items and not edge.get("locked") and doc.get("edge_cases", {}).get("status") not in locked:
            doc["edge_cases"] = {"items": edge_items, "status": "ai"}
            changed += 1
        existing = {q.get("text") for q in doc.get("questions", [])}
        for question in _strs(flow.get("questions"), 2000, 50):
            if question not in existing:
                existing.add(question)
                doc.setdefault("questions", []).append({"id": f"q{len(doc['questions']) + 1}", "text": question, "answer": ""})
                changed += 1
        if changed:
            doc["export_id"] = _str((pack.get("export") if isinstance(pack.get("export"), dict) else {}).get("id"), 100)
            doc["imported_at"] = _now()
            _save_flow(db, item["id"], feature_id, doc)
            applied_parts += changed
            applied_flows.append(item["id"])
    db.commit()
    return {**result, "applied": {"flows": applied_flows, "parts": applied_parts}}


def _import_rule(rule: dict[str, Any], resolve) -> dict[str, Any]:
    at = _str(rule.get("at"), 400)
    evidence = _str(rule.get("evidence"), 400)
    status = rule.get("status") if isinstance(rule.get("status"), str) and rule.get("status") in RULE_STATUSES else "warn"
    if not evidence:
        status = "warn"
    return {"title": _str(rule.get("title"), 400), "text": _str(rule.get("text"), 8000),
            "at": at, "at_node_id": resolve(at) if at else None, "evidence": evidence, "status": status}


# --------------------------------------------------------------------------- search

_NODE_FLOWS: dict[str, Any] = {}


def node_flows(index: GraphIndex, tree: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """node id -> the features and flows that reach it, built once per (graph, tree) instead of per request."""
    key = (id(index), id(tree))
    if _NODE_FLOWS.get("key") == key:
        return _NODE_FLOWS["value"]
    found: dict[str, list[dict[str, str]]] = {}
    for menu in tree["menus"]:
        for feature in menu.get("children", []):
            if feature["kind"] != "feature":
                continue
            for node_id in index.reach([feature["entry_node_id"], *feature.get("extra_entries", [])], MAX_FEATURE_NODES):
                found.setdefault(node_id, []).append({"kind": "feature", "id": feature["id"], "name": feature["name"], "path": menu["name"]})
            for flow in feature["children"]:
                if not flow.get("entry_node_id"):
                    continue
                for node_id in index.reach([flow["entry_node_id"]], MAX_FLOW_NODES):
                    found.setdefault(node_id, []).append({"kind": "flow", "id": flow["id"], "name": flow["name"],
                                                          "path": f"{menu['name']} / {feature['name']}", "feature_id": feature["id"]})
    _NODE_FLOWS.update(key=key, value=found, tree=tree, index=index)  # keep both alive so their ids stay unique
    return found


def node_page(db: sqlite3.Connection, index: GraphIndex, tree: dict[str, Any], node_id: str) -> dict[str, Any]:
    """One code node: its display facts and every documented flow that reaches it."""
    if node_id not in index.nodes:
        raise BusinessError("not_found", "Node not found", 404)
    flows = [{key: hit[key] for key in ("id", "name", "path")} for hit in node_flows(index, tree).get(node_id, []) if hit["kind"] == "flow"]
    return {"node": node_facts(db, index, [node_id])[node_id], "flows": flows}


def search_nodes(db: sqlite3.Connection, index: GraphIndex, tree: dict[str, Any], query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Code nodes matching the query, each with the feature and flow that reach it (if any)."""
    if len(query.strip()) < 2:
        return []
    like = f"%{query.strip()}%"
    rows = db.execute(
        """SELECT node_id FROM graph_nodes WHERE node_type NOT IN ('COLUMN') AND (technical_name LIKE ? COLLATE NOCASE
             OR default_display_name LIKE ? COLLATE NOCASE OR qualified_name LIKE ? COLLATE NOCASE
             OR node_id IN (SELECT target_id FROM graph_localization WHERE value LIKE ? COLLATE NOCASE))
           ORDER BY CASE WHEN technical_name=? COLLATE NOCASE THEN 0 ELSE 1 END, length(technical_name), node_id LIMIT ?""",
        (like, like, like, like, query.strip(), limit),
    ).fetchall()
    reach = node_flows(index, tree)
    results = []
    for (node_id,) in rows:
        if node_id not in index.nodes:
            continue
        node = index.nodes[node_id]
        hit = {"kind": "node", "id": node_id, "label": ref_name(node), "node_type": node["node_type"], "feature_id": None, "flow_id": None}
        owners = reach.get(node_id, [])
        feature = next((owner for owner in owners if owner["kind"] == "feature"), None)
        if feature:
            hit["feature_id"], hit["path"] = feature["id"], feature["name"]
            flow = next((owner for owner in owners if owner["kind"] == "flow" and owner["feature_id"] == feature["id"]), None)
            if flow:
                hit["flow_id"], hit["path"] = flow["id"], f"{feature['name']} / {flow['name']}"
        results.append(hit)
    return results


def search(db: sqlite3.Connection, tree: dict[str, Any], query: str, limit: int = 30) -> list[dict[str, Any]]:
    tokens = [token for token in query.lower().split() if token]
    if not tokens:
        return []
    docs = _docs(db)
    results = []

    def hit(text: str) -> bool:
        lowered = text.lower()
        return all(token in lowered for token in tokens)

    stack = [(item, []) for item in tree["menus"]]
    while stack and len(results) < limit:
        item, trail = stack.pop(0)
        path = " / ".join(trail)
        if item["kind"] in {"menu", "feature"} and hit(f"{item['name']} {item.get('route', '')} {item.get('description', '')}"):
            results.append({"kind": item["kind"], "id": item["id"], "label": item["name"], "path": path, "route": item.get("route", "")})
        if item["kind"] == "flow":
            doc = docs.get(item["id"], {}).get("doc", {})
            if hit(f"{item['name']} {(doc.get('summary') or {}).get('text', '')}"):
                results.append({"kind": "flow", "id": item["id"], "label": item["name"], "path": path, "has_doc": item["has_doc"]})
            for step in doc.get("steps", []):
                if hit(f"{step.get('name', '')} {step.get('text', '')}"):
                    results.append({"kind": "step", "id": item["id"], "step_id": step["id"], "label": step.get("name") or step["id"],
                                    "path": f"{path} / {item['name']}"})
                for rule in step.get("rules", []):
                    if hit(f"{rule.get('title', '')} {rule.get('text', '')}"):
                        results.append({"kind": "rule", "id": item["id"], "step_id": step["id"], "label": rule.get("title", ""),
                                        "path": f"{path} / {item['name']}"})
        stack.extend((child, [*trail, item["name"]]) for child in item.get("children", []))
    return results[:limit]
