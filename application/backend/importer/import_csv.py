from __future__ import annotations

import csv
import io
from pathlib import Path

from application.backend.config import CSV_ENCODINGS
from contract.entities import ExtractionContext, MetadataFact
_FIELD_ALIASES = {
    "db_name", "db", "schema_name", "schema",
    "table_code", "table", "table_name", "mã table",
    "table_name_ja", "table_ja", "tên tiếng nhật table",
    "tên nhật table", "tên tiếng nhật", "table_name_en", "table_en",
    "tên tiếng anh table", "tên anh table", "tên tiếng anh",
    "table_description", "table_desc", "description table", "description",
    "column_code", "column", "column_name", "mã column",
    "column_name_ja", "column_ja", "tên tiếng nhật column",
    "tên nhật column", "column_name_en", "column_en",
    "tên tiếng anh column", "tên anh column", "column_description",
    "column_desc", "description column", "comment", "note",
}


def read_csv(
    data: bytes, path: Path, encoding: str = "utf-8"
) -> list[dict[str, str | None]]:
    if encoding not in CSV_ENCODINGS:
        raise ValueError(f"{path}: unsupported CSV encoding: {encoding}")
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as error:
        raise ValueError(f"{path}: invalid {encoding} CSV") from error
    text = text.removeprefix("\ufeff")
    if not text:
        raise ValueError(f"{path}: CSV header is required")

    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = reader.fieldnames
    if not headers:
        raise ValueError(f"{path}: CSV header is required")
    normalized_headers = [str(header or "").strip().lower() for header in headers]
    if any(not header for header in normalized_headers):
        raise ValueError(f"{path}: CSV headers must be non-empty")
    if len(normalized_headers) != len(set(normalized_headers)):
        raise ValueError(f"{path}: duplicate normalized CSV headers")
    if not set(normalized_headers) & _FIELD_ALIASES:
        raise ValueError(f"{path}: CSV has no recognized metadata header")

    rows = list(reader)
    for row_number, row in enumerate(rows, 2):
        if None in row:
            raise ValueError(f"{path}:{row_number}: CSV row has extra columns")
    return rows


def val(row: dict[str, str | None], *names: str) -> str:
    lookup = {
        str(key).strip().lower(): (value or "").strip()
        for key, value in row.items()
    }
    return next((lookup[name.lower()] for name in names if name.lower() in lookup), "")


def csv_files(path: Path) -> list[Path]:
    path = path.expanduser().resolve()
    return sorted(path.rglob("*.csv")) if path.is_dir() else [path]


def inferred_table_code(path: Path) -> str:
    return "" if path.stem.lower() in {"tables", "table_definitions"} else path.stem


def parse_metadata_facts(
    data: bytes,
    path: Path,
    defaults: dict[str, str] | None = None,
    *,
    encoding: str = "utf-8",
) -> list[MetadataFact]:
    """Parse supplied stable bytes without opening or mutating external state."""
    defaults = defaults or {}
    default_db_name = str(defaults.get("db_name", "")).strip()
    default_schema = str(defaults.get("schema", "")).strip()
    default_table_code = inferred_table_code(path)
    folded: dict[tuple[str, str], MetadataFact] = {}

    def fold(
        kind: str, entity_id: str, payload: dict[str, str], row_order: int
    ) -> None:
        existing = folded.get((kind, entity_id))
        merged = dict(existing.payload) if existing else {}
        merged.update({key: value for key, value in payload.items() if value})
        folded[(kind, entity_id)] = MetadataFact(
            kind, entity_id, merged, row_order
        )

    for row_order, row in enumerate(read_csv(data, path, encoding), 1):
        if not any((value or "").strip() for value in row.values()):
            continue
        db_name = val(row, "db_name", "db") or default_db_name
        schema_name = val(row, "schema_name", "schema") or default_schema
        table_code = (
            val(row, "table_code", "table", "table_name", "mã table")
            or default_table_code
        )
        if not db_name or not schema_name or not table_code:
            raise ValueError(
                f"{path}:{row_order + 1}: CSV metadata requires "
                "db_name, schema, and table"
            )

        context = ExtractionContext(db_name=db_name.strip(), schema_name=schema_name)
        canonical_schema, canonical_table, _ = context.resolved_object(table_code)
        table_id = context.table_qname(table_code).removeprefix("Table:")
        fold(
            "table",
            table_id,
            {
                "db_name": context.db_name,
                "schema_name": canonical_schema,
                "table_name": canonical_table,
                "code": canonical_table,
                "name_ja": val(
                    row,
                    "table_name_ja",
                    "table_ja",
                    "tên tiếng nhật table",
                    "tên nhật table",
                    "tên tiếng nhật",
                ),
                "name_en": val(
                    row,
                    "table_name_en",
                    "table_en",
                    "tên tiếng anh table",
                    "tên anh table",
                    "tên tiếng anh",
                ),
                "description": val(
                    row,
                    "table_description",
                    "table_desc",
                    "description table",
                    "description",
                ),
            },
            row_order,
        )

        column_code = val(
            row, "column_code", "column", "column_name", "mã column"
        )
        if not column_code:
            continue
        column_id = context.column_qname(table_code, column_code)
        canonical_column = column_id.rsplit(":", 1)[-1]
        fold(
            "column",
            column_id,
            {
                "db_name": context.db_name,
                "schema_name": canonical_schema,
                "table_name": canonical_table,
                "code": canonical_column,
                "name": canonical_column,
                "name_ja": val(
                    row,
                    "column_name_ja",
                    "column_ja",
                    "tên tiếng nhật column",
                    "tên nhật column",
                ),
                "name_en": val(
                    row,
                    "column_name_en",
                    "column_en",
                    "tên tiếng anh column",
                    "tên anh column",
                ),
                "description": val(
                    row,
                    "column_description",
                    "column_desc",
                    "description column",
                    "note",
                ),
                "comment": val(row, "comment"),
            },
            row_order,
        )

    return sorted(
        folded.values(),
        key=lambda fact: (fact.row_order, fact.entity_kind, fact.entity_id),
    )
