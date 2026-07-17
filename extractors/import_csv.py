from __future__ import annotations

import argparse
import csv
import os
import sqlite3
from pathlib import Path

from common.config import ROOT, load_extractor_config, resolve_path
from db.writer import ensure_db_schema

ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc_jp")


def read_csv(path: Path) -> list[dict[str, str]]:
    data = path.read_bytes()
    for encoding in ENCODINGS:
        try:
            return list(csv.DictReader(data.decode(encoding).splitlines()))
        except UnicodeDecodeError:
            pass
    return list(csv.DictReader(data.decode("utf-8", errors="replace").splitlines()))


def val(row: dict[str, str], *names: str) -> str:
    lookup = {str(k).strip().lower(): (v or "").strip() for k, v in row.items()}
    return next((lookup[name.lower()] for name in names if name.lower() in lookup), "")


def csv_files(path: Path) -> list[Path]:
    path = path.expanduser().resolve()
    return sorted(path.rglob("*.csv")) if path.is_dir() else [path]


def inferred_table_code(path: Path) -> str:
    return (
        ""
        if path.stem.lower() in {"tables", "table_definitions"}
        else path.stem.upper()
    )


def import_csv(csv_path: Path, config_path: Path) -> tuple[int, int]:
    config_path = config_path.expanduser().resolve()
    config = load_extractor_config(config_path)
    db_path = resolve_path(config["db"], config_path.parent)
    defaults = config["table_definition"]
    tables: set[tuple[str, str, str]] = set()
    columns = 0
    with sqlite3.connect(db_path) as db:
        ensure_db_schema(db)
        for file_path in csv_files(csv_path):
            default_table_code = inferred_table_code(file_path)
            for row in read_csv(file_path):
                db_name = val(row, "db_name", "db") or defaults["db_name"]
                schema_name = val(row, "schema_name", "schema") or defaults["schema"]
                table_code = (
                    val(row, "table_code", "table", "table_name", "mã table")
                    or default_table_code
                )
                column_code = val(
                    row, "column_code", "column", "column_name", "mã column"
                )
                if not table_code:
                    continue
                db.execute(
                    """
                    INSERT INTO table_definitions(id, db_name, schema_name, table_name, code, name_ja, name_en, description)
                    VALUES(?,?,?,?,?,?,?,?)
                                        ON CONFLICT(id) DO UPDATE SET
                                            name_ja=COALESCE(NULLIF(excluded.name_ja, ''), table_definitions.name_ja),
                                            name_en=COALESCE(NULLIF(excluded.name_en, ''), table_definitions.name_en),
                                            description=COALESCE(NULLIF(excluded.description, ''), table_definitions.description)
                    """,
                    (
                        f"{db_name}:{schema_name}.{table_code}",
                        db_name,
                        schema_name,
                        table_code,
                        table_code,
                        val(
                            row,
                            "table_name_ja",
                            "table_ja",
                            "tên tiếng nhật table",
                            "tên nhật table",
                            "tên tiếng nhật",
                        ),
                        val(
                            row,
                            "table_name_en",
                            "table_en",
                            "tên tiếng anh table",
                            "tên anh table",
                            "tên tiếng anh",
                        ),
                        val(
                            row,
                            "table_description",
                            "table_desc",
                            "description table",
                            "description",
                        ),
                    ),
                )
                tables.add((db_name, schema_name, table_code))
                if not column_code:
                    continue
                column_id = f"Column:{db_name}:{schema_name}.{table_code}:{column_code}"
                db.execute(
                    "DELETE FROM table_columns WHERE id=?",
                    (f"Column:{db_name}:{schema_name}.{table_code}.{column_code}",),
                )
                db.execute(
                    """
                    INSERT INTO table_columns(id, db_name, schema_name, table_name, code, name, name_ja, name_en, description, comment)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                                        ON CONFLICT(id) DO UPDATE SET
                                            code=excluded.code,
                                            name=excluded.name,
                                            name_ja=COALESCE(NULLIF(excluded.name_ja, ''), table_columns.name_ja),
                                            name_en=COALESCE(NULLIF(excluded.name_en, ''), table_columns.name_en),
                                            description=COALESCE(NULLIF(excluded.description, ''), table_columns.description),
                                            comment=COALESCE(NULLIF(excluded.comment, ''), table_columns.comment)
                    """,
                    (
                        column_id,
                        db_name,
                        schema_name,
                        table_code,
                        column_code,
                        column_code,
                        val(
                            row,
                            "column_name_ja",
                            "column_ja",
                            "tên tiếng nhật column",
                            "tên nhật column",
                        ),
                        val(
                            row,
                            "column_name_en",
                            "column_en",
                            "tên tiếng anh column",
                            "tên anh column",
                        ),
                        val(
                            row,
                            "column_description",
                            "column_desc",
                            "description column",
                        ),
                        val(row, "comment"),
                    ),
                )
                columns += 1
        db.commit()
    return len(tables), columns


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import table/column metadata CSV files into SQLite."
    )
    parser.add_argument("csv", help="CSV file path or directory")
    parser.add_argument(
        "--config", default=os.environ.get("CODE_MAP_CONFIG", "code-map.config.json")
    )
    args = parser.parse_args()
    tables, columns = import_csv(Path(args.csv), resolve_path(args.config, ROOT))
    print(f"Imported: tables={tables} columns={columns}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
