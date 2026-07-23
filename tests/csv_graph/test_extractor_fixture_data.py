from __future__ import annotations

import csv
import unittest
from pathlib import Path

from contract import schema as S
from contract.entities import ExtractionContext
from extractors.csharp_sql.extractor import CSharpSqlExtractor
from extractors.sql.references import extract_sql_references

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOTS = (
    ROOT / "output/angular/customer-web",
    ROOT / "output/dotnet-api/order-api",
    ROOT / "output/batch/order-fulfillment",
    ROOT / "output/plsql/order-db",
    ROOT / "output/sql-files/order-ops",
)

EVIDENCE_TOKENS = {
    "ev:create-ui": ("submitOrder", "(click)"),
    "ev:create-http": ("this.http.post", "this.config.createOrder"),
    "ev:jobnet": ("ORDER_FULFILLMENT", "ALLOCATE_ORDERS", "CREATE_SHIPMENTS", "RECONCILE_ORDERS"),
    "ev:batch-entry": ("mode", "Allocate", "CreateShipments", "Reconcile"),
    "ev:create-api": ("HttpPost action calls OrderService.CreateOrder", "_service.CreateOrder"),
    "ev:create-service": ("CreateOrder", "_repository.CreateOrder"),
    "ev:read-api": ("ORDER_HEADER", "ORDER_LINE", "PAYMENT_TRANSACTION"),
    "ev:idempotency": ("HttpPost(RetryRoute)", "_service.CreateOrder"),
    "ev:create-proc": ("CREATE_ORDER", "ORDER_HEADER", "ORDER_LINE", "INVENTORY_RESERVATION", "PAYMENT_TRANSACTION"),
    "ev:cancel": ("UPDATE ORDER_HEADER", "DELETE FROM INVENTORY_RESERVATION", "SHIPMENT_AUDIT"),
    "ev:allocate": ("ORDER_HEADER", "FULFILLMENT_ALLOCATION", "BACKORDER", "EXECUTE IMMEDIATE", "SHIPMENT_PACKAGE"),
    "ev:shipment-trigger": ("TRG_SHIPMENT_AUDIT", "INSERT INTO SHIPMENT_AUDIT", "Shipment status change inserts audit row"),
    "ev:tax-link": ("TAX_SCHEMA.TAX_RATE@TAX_DB_LINK",),
    "ev:dynamic": ("EXECUTE IMMEDIATE v_sql",),
    "ev:column-status": ("UPDATE ORDER_HEADER SET STATUS = :status",),
    "ev:payment-failure": ("DUP_VAL_ON_INDEX", "PAYMENT_TRANSACTION", "FAILED"),
    "ev:partial": ("BACKORDER", "BACKORDER_SEQ"),
    "ev:reconcile-read": ("ORDER_HEADER", "SHIPMENT"),
    "ev:reconcile-merge": ("MERGE INTO ORDER_DAILY_SUMMARY",),
    "ev:direct-writer": ("MERGE INTO ORDER_DAILY_SUMMARY",),
}

ISSUE_TOKENS = {
    "issue:dynamic-config:1": ("this.config[endpointName]",),
    "issue:api-not-matched:1": ("/legacy/orders",),
    "issue:api-ambiguous:1": ("GET /orders/{id}",),
    "issue:executable-not-mapped:1": ("PartnerExport.exe",),
    "issue:table-not-imported:1": ("ORDER_ARCHIVE_TMP",),
    "issue:column-not-imported:1": ("ORDER_HEADER.LEGACY_STATUS",),
    "issue:procedure-not-found:1": ("PKG_ORDER.SUBMIT_LEGACY",),
    "issue:dynamic-sql:1": ("EXECUTE IMMEDIATE v_sql",),
    "issue:external-object:1": ("TAX_SCHEMA.TAX_RATE@TAX_DB_LINK",),
    "issue:parse-error:1": ("SELECT FROM WHERE MALFORMED",),
    "issue:invalid-config:1": ("missing database mapping",),
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _line_range(source_path: str, start_line: str, end_line: str | None = None) -> str:
    path = ROOT / source_path
    lines = path.read_text(encoding="utf-8").splitlines()
    start = int(start_line)
    end = int(end_line or start_line)
    if start < 1 or end > len(lines) or start > end:
        raise AssertionError(f"Invalid line range {source_path}:{start}-{end}; file has {len(lines)} lines")
    return "\n".join(lines[start - 1:end])


class ExtractorFixtureDataTest(unittest.TestCase):
    def test_package_evidence_points_to_real_source_anchors(self):
        seen: set[str] = set()
        for root in PACKAGE_ROOTS:
            for row in _read_csv(root / "evidence.csv"):
                seen.add(row["evidence_id"])
                path = ROOT / row["source_path"]
                self.assertTrue(path.is_file(), row["source_path"])
                excerpt = _line_range(row["source_path"], row["start_line"], row["end_line"])
                tokens = EVIDENCE_TOKENS[row["evidence_id"]]
                for token in tokens:
                    self.assertIn(token, excerpt, f"{row['evidence_id']} missing {token!r}")
        self.assertEqual(set(EVIDENCE_TOKENS), seen)

    def test_package_issues_point_to_real_source_anchors(self):
        seen: set[str] = set()
        for root in PACKAGE_ROOTS:
            for row in _read_csv(root / "issues.csv"):
                if not row["source_path"]:
                    continue
                seen.add(row["issue_id"])
                excerpt = _line_range(row["source_path"], row["start_line"])
                tokens = ISSUE_TOKENS[row["issue_id"]]
                for token in tokens:
                    self.assertIn(token, excerpt, f"{row['issue_id']} missing {token!r}")
        self.assertEqual(set(ISSUE_TOKENS), seen)

    def test_authoritative_csvs_cover_resolver_and_negative_cases(self):
        table_rows = _read_csv(ROOT / "input-data/tables.csv")
        tables = {row["table_code"] for row in table_rows}
        table_columns = {(row["table_code"], column["column_code"]) for row in table_rows for column in _read_csv(ROOT / "input-data/tables" / f"{row['table_code']}.csv")}
        for table in (
            "ORDER_HEADER",
            "ORDER_LINE",
            "INVENTORY_RESERVATION",
            "PAYMENT_TRANSACTION",
            "FULFILLMENT_ALLOCATION",
            "BACKORDER",
            "SHIPMENT",
            "SHIPMENT_PACKAGE",
            "SHIPMENT_AUDIT",
            "ORDER_DAILY_SUMMARY",
        ):
            self.assertIn(table, tables)
        self.assertIn(("ORDER_HEADER", "STATUS"), table_columns)
        self.assertNotIn("ORDER_ARCHIVE_TMP", tables)
        self.assertNotIn(("ORDER_HEADER", "LEGACY_STATUS"), table_columns)

        jobs = _read_csv(ROOT / "input-data/jobnet.csv")
        self.assertEqual(["ALLOCATE_ORDERS", "CREATE_SHIPMENTS", "RECONCILE_ORDERS", "UNMAPPED_EXPORT"], [row["job_id"] for row in jobs])
        self.assertEqual("ALLOCATE_ORDERS", jobs[1]["predecessor_job_id"])
        self.assertEqual("RECONCILE_ORDERS", jobs[3]["predecessor_job_id"])

        mappings = _read_csv(ROOT / "input-data/executable-mappings.csv")
        mapped = {row["executable_name"] for row in mappings}
        self.assertEqual({"OrderFulfillment.exe", "OrderReconcile.exe"}, mapped)
        self.assertNotIn("PartnerExport.exe", mapped)

        localized = _read_csv(ROOT / "input-data/localized-metadata.csv")
        locales = {(row["target_id"], row["field_name"], row["locale"]) for row in localized}
        self.assertIn(("table:ORDER_DB:ORDER_HEADER", "name", "ja"), locales)
        self.assertIn(("column:ORDER_DB:ORDER_HEADER:STATUS", "name", "en"), locales)

    def test_sql_fixture_exposes_reads_writes_and_remote_reference(self):
        sql_text = (ROOT / "demo-sources/order-ops/reconcile_orders.sql").read_text(encoding="utf-8")
        sql_names = {ref.object_name.upper() for ref in extract_sql_references(sql_text)}
        self.assertIn("ORDER_HEADER", sql_names)
        self.assertIn("SHIPMENT", sql_names)
        self.assertIn("ORDER_DAILY_SUMMARY", sql_names)

        plsql_text = (ROOT / "demo-sources/order-db/order_flow.sql").read_text(encoding="utf-8")
        plsql_names = {ref.object_name.upper() for ref in extract_sql_references(plsql_text)}
        for name in (
            "ORDER_HEADER",
            "ORDER_LINE",
            "INVENTORY_RESERVATION",
            "PAYMENT_TRANSACTION",
            "FULFILLMENT_ALLOCATION",
            "BACKORDER",
            "SHIPMENT_PACKAGE",
            "TAX_SCHEMA.TAX_RATE@TAX_DB_LINK",
        ):
            self.assertIn(name, plsql_names)

    def test_csharp_fixture_exposes_sql_tables_and_stored_procedure_calls(self):
        extractor = CSharpSqlExtractor()
        context = ExtractionContext(
            repository="order-demo",
            db_name="ORDER_DB",
            schema_name="ORDER_APP",
            source_id="demo",
            relative_source_path="demo-sources/order-api/OrderFlow.cs",
            project_name="order-api",
        )
        api_text = (ROOT / "demo-sources/order-api/OrderFlow.cs").read_text(encoding="utf-8")
        result = extractor.extract("OrderFlow.cs", api_text, context)
        edge_targets = {edge.to_key_value for edge in result.edges}
        self.assertTrue(any("ORDER_HEADER" in target for target in edge_targets))
        self.assertTrue(any("ORDER_LINE" in target for target in edge_targets))
        self.assertTrue(any("PAYMENT_TRANSACTION" in target for target in edge_targets))
        self.assertTrue(any("ORDER_ARCHIVE_TMP" in target for target in edge_targets))
        self.assertTrue(any("PKG_ORDER.CREATE_ORDER" in target for target in edge_targets))
        self.assertTrue(any("PKG_ORDER.CANCEL_ORDER" in target for target in edge_targets))
        self.assertTrue(any("PKG_ORDER.SUBMIT_LEGACY" in target for target in edge_targets))
        self.assertIn(S.REL_CALLS, {edge.rel_type for edge in result.edges})

        batch_context = ExtractionContext(
            repository="order-demo",
            db_name="ORDER_DB",
            schema_name="ORDER_APP",
            source_id="demo",
            relative_source_path="demo-sources/order-fulfillment/Program.cs",
            project_name="order-fulfillment",
        )
        batch_text = (ROOT / "demo-sources/order-fulfillment/Program.cs").read_text(encoding="utf-8")
        batch_result = extractor.extract("Program.cs", batch_text, batch_context)
        batch_targets = {edge.to_key_value for edge in batch_result.edges}
        self.assertTrue(any("PKG_ORDER.ALLOCATE_ORDER" in target for target in batch_targets))
        self.assertTrue(any("SHIPMENT" in target for target in batch_targets))
        self.assertTrue(any("ORDER_DAILY_SUMMARY" in target for target in batch_targets))


if __name__ == "__main__":
    unittest.main()
