"""Generate a multi-system source corpus whose every dependency is known.

One business domain (invoice, shipment, …) becomes, across five systems:

* ``web``    Angular screens and a service calling the API through appconfig.json;
* ``api-*``  ASP.NET controllers → services → repositories (embedded Oracle SQL, stored procedures);
* ``batch``  a .NET batch with one ``--mode`` per domain, started by jobnet.csv;
* ``db``     a PL/SQL package, a trigger, a DB-link read and cross-package calls;
* ``ops``    SQL report files and a SQL*Loader control file.

Each dependency is written by a *pattern* (``PATTERNS``) and recorded in
``truth/edges.jsonl`` with what static extraction is expected to see:
``detectable`` (a miss is a regression), ``partial`` (only an issue or a
reference is expected) or ``undetectable`` (a known blind spot, measured so it
is never presented as covered). ``scenarios/*.yaml`` are change requests with
their true impact, for scoring impact analyses (``score impact``).
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE_DOMAINS = [
    "invoice", "shipment", "customer", "product", "payment", "stock", "refund", "coupon",
    "supplier", "warehouse", "contract", "subscription",
]
SIZES = {"s": 3, "m": 12, "l": 60}

# pattern -> (expected, description)
PATTERNS: dict[str, tuple[str, str]] = {
    "plsql_insert_values": ("detectable", "INSERT ... VALUES in a package procedure"),
    "plsql_insert_select": ("detectable", "INSERT ... SELECT reads one table, writes another"),
    "plsql_update": ("detectable", "UPDATE with WHERE"),
    "plsql_delete": ("detectable", "DELETE"),
    "plsql_merge": ("detectable", "MERGE INTO ... USING"),
    "plsql_select_into": ("detectable", "SELECT ... INTO a variable"),
    "plsql_cursor_loop": ("detectable", "FOR r IN (SELECT ...) LOOP with DML inside"),
    "plsql_local_call": ("detectable", "call of a procedure in the same package"),
    "plsql_cross_package_call": ("detectable", "call PKG_OTHER.PROC across packages"),
    "plsql_sequence": ("detectable", "SEQ.NEXTVAL"),
    "plsql_dblink_read": ("detectable", "SELECT ... FROM T@LINK (remote system)"),
    "plsql_trigger": ("detectable", "AFTER UPDATE trigger writing an audit table"),
    "plsql_dynamic_concat": ("undetectable", "EXECUTE IMMEDIATE with the table name concatenated"),
    "plsql_dynamic_literal": ("partial", "EXECUTE IMMEDIATE of a literal statement held in a variable"),
    "sqlfile_report": ("detectable", "report SQL file joining two tables"),
    "sqlfile_loader": ("detectable", "SQL*Loader control file loading a staging table"),
    "dotnet_const_sql": ("detectable", "const string SELECT in a repository method"),
    "dotnet_verbatim_dml": ("detectable", "verbatim @\"UPDATE ...\" in a repository method"),
    "dotnet_stored_procedure": ("detectable", "CommandType.StoredProcedure with CommandText PKG.PROC"),
    "dotnet_stringbuilder_sql": ("partial", "SQL assembled with StringBuilder"),
    "dotnet_interpolated_table": ("undetectable", "interpolated table name $\"... {table}\""),
    "dotnet_controller_route": ("detectable", "[Route] + [HttpGet/Post/Delete] controller actions"),
    "dotnet_minimal_api": ("detectable", "app.MapGet minimal API endpoint"),
    "dotnet_service_call": ("detectable", "controller → service → repository calls"),
    "batch_mode_dispatch": ("detectable", "batch Main dispatching on --mode"),
    "jobnet_job": ("detectable", "jobnet.csv job starting the batch with --mode"),
    "angular_route_screen": ("detectable", "route → component screen"),
    "angular_template_action": ("detectable", "(click) handler in the component template"),
    "angular_config_url": ("detectable", "HttpClient call with this.config.<key>"),
    "angular_template_url": ("detectable", "URL built as `${this.config.key}/${id}`"),
    "angular_request_method": ("partial", "http.request(method variable, url)"),
}


@dataclass
class Domain:
    key: str          # invoice
    index: int
    api: str          # api-sales

    @property
    def upper(self) -> str:
        return self.key.upper()

    @property
    def pascal(self) -> str:
        return "".join(part.capitalize() for part in self.key.split("_"))

    @property
    def plural(self) -> str:
        return self.key + ("es" if self.key.endswith(("s", "x", "ch")) else "s")

    @property
    def package(self) -> str:
        return f"PKG_{self.upper}"

    def table(self, suffix: str) -> str:
        return f"{self.upper}_{suffix}"


@dataclass
class Corpus:
    root: Path
    size: str
    seed: int
    domains: list[Domain] = field(default_factory=list)
    truth: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    tables: dict[str, list[str]] = field(default_factory=dict)
    jobs: list[dict[str, str]] = field(default_factory=list)

    def edge(self, pattern: str, source: tuple[str, str], kind: str, target: tuple[str, str], file: str) -> None:
        expected, _ = PATTERNS[pattern]
        self.truth.append({
            "id": f"t{len(self.truth) + 1}", "pattern": pattern, "expected": expected,
            "source": {"kind": source[0], "name": source[1]}, "class": kind,
            "target": {"kind": target[0], "name": target[1]}, "file": file,
        })

    def write(self, relative: str, text: str) -> None:
        self.files[relative] = text


def generate(root: Path, size: str = "s", seed: int = 1) -> Corpus:
    if size not in SIZES:
        raise ValueError(f"size must be one of {', '.join(SIZES)}")
    randomizer = random.Random(seed)
    count = SIZES[size]
    names = BASE_DOMAINS[:count] + [f"area{number}" for number in range(count - len(BASE_DOMAINS))]
    apis = ["api-sales"] if size == "s" else ["api-sales", "api-logistics"]
    corpus = Corpus(root=root, size=size, seed=seed)
    corpus.domains = [Domain(name, index, apis[index % len(apis)]) for index, name in enumerate(names)]
    for domain in corpus.domains:
        following = corpus.domains[(domain.index + 1) % len(corpus.domains)]
        _plsql(corpus, domain, following, randomizer)
        _ops(corpus, domain)
        _dotnet_api(corpus, domain)
        _angular(corpus, domain)
    _angular_shell(corpus)
    _batch(corpus)
    _inputs(corpus, apis)
    _scenarios(corpus)
    _write(corpus, apis)
    return corpus


# --------------------------------------------------------------------------- PL/SQL

def _plsql(corpus: Corpus, d: Domain, following: Domain, randomizer: random.Random) -> None:
    header, line, history, staging = d.table("HEADER"), d.table("LINE"), d.table("HISTORY"), d.table("STAGING")
    corpus.tables.update({
        header: ["ID", "STATUS", "AMOUNT", "CUSTOMER_ID", "CREATED_AT"],
        line: ["ID", "HEADER_ID", "QUANTITY", "PRICE"],
        history: ["ID", "HEADER_ID", "STATUS", "CHANGED_AT"],
        staging: ["ID", "HEADER_ID", "QUANTITY", "PRICE", "LOADED_AT"],
    })
    file = f"db/{d.key}_pkg.sql"
    pkg = d.package

    def proc(name: str) -> tuple[str, str]:
        return ("procedure", f"{pkg}.{name}")

    create, cancel, sync, rebuild, notify, total = (
        f"CREATE_{d.upper}", f"CANCEL_{d.upper}", f"SYNC_{d.upper}", f"REBUILD_{d.upper}", f"NOTIFY_{d.upper}",
        f"TOTAL_{d.upper}",
    )
    validate = f"VALIDATE_{d.upper}"
    corpus.edge("plsql_sequence", proc(create), "read", ("sequence", f"{d.upper}_SEQ"), file)
    corpus.edge("plsql_insert_values", proc(create), "write", ("table", header), file)
    corpus.edge("plsql_insert_select", proc(create), "write", ("table", line), file)
    corpus.edge("plsql_insert_select", proc(create), "read", ("table", staging), file)
    corpus.edge("plsql_local_call", proc(create), "call", proc(validate), file)
    corpus.edge("plsql_cross_package_call", proc(create), "call", ("procedure", f"{following.package}.NOTIFY_{following.upper}"), file)
    corpus.edge("plsql_select_into", proc(validate), "read", ("table", staging), file)
    corpus.edge("plsql_update", proc(cancel), "write", ("table", header), file)
    corpus.edge("plsql_delete", proc(cancel), "write", ("table", line), file)
    corpus.edge("plsql_insert_values", proc(cancel), "write", ("table", history), file)
    corpus.edge("plsql_merge", proc(sync), "write", ("table", header), file)
    corpus.edge("plsql_merge", proc(sync), "read", ("table", staging), file)
    corpus.edge("plsql_cursor_loop", proc(sync), "read", ("table", line), file)
    corpus.edge("plsql_cursor_loop", proc(sync), "write", ("table", history), file)
    corpus.edge("plsql_dynamic_concat", proc(rebuild), "write", ("table", staging), file)
    corpus.edge("plsql_dynamic_literal", proc(rebuild), "write", ("table", history), file)
    corpus.edge("plsql_select_into", proc(total), "read", ("table", line), file)
    corpus.edge("plsql_dblink_read", proc(notify), "read", ("external", f"LEGACY_{d.upper}_EVENTS"), file)
    corpus.edge("plsql_insert_values", proc(notify), "write", ("table", history), file)
    corpus.edge("plsql_trigger", ("trigger", f"TRG_{d.upper}_AUDIT"), "write", ("table", history), file)
    corpus.edge("plsql_trigger", ("trigger", f"TRG_{d.upper}_AUDIT"), "read", ("table", header), file)
    corpus.edge("plsql_sequence", ("trigger", f"TRG_{d.upper}_AUDIT"), "read", ("sequence", f"{d.upper}_SEQ"), file)
    for name in (cancel, sync, notify):
        corpus.edge("plsql_sequence", proc(name), "read", ("sequence", f"{d.upper}_SEQ"), file)
    threshold = randomizer.choice([100, 500, 1000, 5000])
    corpus.write(file, f"""-- {d.pascal} domain: package {pkg} and audit trigger
CREATE OR REPLACE PACKAGE {pkg} AS
\tPROCEDURE {create}(p_customer_id IN NUMBER);
\tPROCEDURE {cancel}(p_id IN NUMBER);
\tPROCEDURE {sync};
\tPROCEDURE {rebuild}(p_suffix IN VARCHAR2);
\tPROCEDURE {notify}(p_id IN NUMBER);
\tFUNCTION {total}(p_id IN NUMBER) RETURN NUMBER;
END {pkg};
/

CREATE OR REPLACE PACKAGE BODY {pkg} AS

\tPROCEDURE {validate}(p_customer_id IN NUMBER) IS
\t\tv_count NUMBER;
\tBEGIN
\t\tSELECT COUNT(*) INTO v_count FROM {staging} WHERE HEADER_ID = p_customer_id;
\t\tIF v_count = 0 THEN
\t\t\tRAISE_APPLICATION_ERROR(-20001, 'No lines to {d.key}');
\t\tEND IF;
\tEND {validate};

\tPROCEDURE {create}(p_customer_id IN NUMBER) IS
\t\tv_id NUMBER;
\tBEGIN
\t\t{validate}(p_customer_id);
\t\tv_id := {d.upper}_SEQ.NEXTVAL;
\t\tINSERT INTO {header} (ID, STATUS, AMOUNT, CUSTOMER_ID, CREATED_AT)
\t\tVALUES (v_id, 'NEW', 0, p_customer_id, SYSDATE);
\t\tINSERT INTO {line} (ID, HEADER_ID, QUANTITY, PRICE)
\t\tSELECT ID, v_id, QUANTITY, PRICE FROM {staging} WHERE HEADER_ID = p_customer_id;
\t\t{following.package}.NOTIFY_{following.upper}(v_id);
\tEND {create};

\tPROCEDURE {cancel}(p_id IN NUMBER) IS
\tBEGIN
\t\tUPDATE {header} SET STATUS = 'CANCELLED' WHERE ID = p_id AND STATUS <> 'CLOSED';
\t\tIF SQL%ROWCOUNT = 0 THEN
\t\t\tRAISE_APPLICATION_ERROR(-20002, '{d.pascal} is closed');
\t\tEND IF;
\t\tDELETE FROM {line} WHERE HEADER_ID = p_id;
\t\tINSERT INTO {history} (ID, HEADER_ID, STATUS, CHANGED_AT) VALUES ({d.upper}_SEQ.NEXTVAL, p_id, 'CANCELLED', SYSDATE);
\tEND {cancel};

\tPROCEDURE {sync} IS
\tBEGIN
\t\tMERGE INTO {header} dst
\t\tUSING (SELECT HEADER_ID, SUM(QUANTITY * PRICE) AS AMOUNT FROM {staging} GROUP BY HEADER_ID) src
\t\tON (dst.ID = src.HEADER_ID)
\t\tWHEN MATCHED THEN UPDATE SET dst.AMOUNT = src.AMOUNT;
\t\tFOR r IN (SELECT HEADER_ID, SUM(QUANTITY) AS QTY FROM {line} GROUP BY HEADER_ID) LOOP
\t\t\tIF r.QTY > {threshold} THEN
\t\t\t\tINSERT INTO {history} (ID, HEADER_ID, STATUS, CHANGED_AT) VALUES ({d.upper}_SEQ.NEXTVAL, r.HEADER_ID, 'LARGE', SYSDATE);
\t\t\tEND IF;
\t\tEND LOOP;
\tEND {sync};

\tPROCEDURE {rebuild}(p_suffix IN VARCHAR2) IS
\t\tv_sql VARCHAR2(4000);
\tBEGIN
\t\tEXECUTE IMMEDIATE 'DELETE FROM {d.upper}_' || p_suffix;
\t\tv_sql := 'INSERT INTO {history} (ID, HEADER_ID, STATUS, CHANGED_AT) SELECT ID, HEADER_ID, ''REBUILT'', SYSDATE FROM {line}';
\t\tEXECUTE IMMEDIATE v_sql;
\tEND {rebuild};

\tPROCEDURE {notify}(p_id IN NUMBER) IS
\t\tv_events NUMBER;
\tBEGIN
\t\tSELECT COUNT(*) INTO v_events FROM LEGACY_{d.upper}_EVENTS@LEGACY_LINK WHERE REF_ID = p_id;
\t\tINSERT INTO {history} (ID, HEADER_ID, STATUS, CHANGED_AT) VALUES ({d.upper}_SEQ.NEXTVAL, p_id, 'NOTIFIED', SYSDATE);
\tEND {notify};

\tFUNCTION {total}(p_id IN NUMBER) RETURN NUMBER IS
\t\tv_total NUMBER;
\tBEGIN
\t\tSELECT SUM(QUANTITY * PRICE) INTO v_total FROM {line} WHERE HEADER_ID = p_id;
\t\tRETURN NVL(v_total, 0);
\tEND {total};

END {pkg};
/

CREATE OR REPLACE TRIGGER TRG_{d.upper}_AUDIT
AFTER UPDATE OF STATUS ON {header}
FOR EACH ROW
BEGIN
\tINSERT INTO {history} (ID, HEADER_ID, STATUS, CHANGED_AT) VALUES ({d.upper}_SEQ.NEXTVAL, :NEW.ID, :NEW.STATUS, SYSDATE);
END;
/
""")


def _ops(corpus: Corpus, d: Domain) -> None:
    report = f"ops/{d.key}_report.sql"
    loader = f"ops/load_{d.key}.ctl"
    corpus.edge("sqlfile_report", ("sql_file", f"{d.key}_report.sql"), "read", ("table", d.table("HEADER")), report)
    corpus.edge("sqlfile_report", ("sql_file", f"{d.key}_report.sql"), "read", ("table", d.table("LINE")), report)
    corpus.edge("sqlfile_loader", ("loader", f"load_{d.key}.ctl"), "write", ("table", d.table("STAGING")), loader)
    corpus.write(report, f"""-- Daily {d.key} report
SELECT h.ID, h.STATUS, SUM(l.QUANTITY * l.PRICE) AS TOTAL
  FROM {d.table('HEADER')} h
  JOIN {d.table('LINE')} l ON l.HEADER_ID = h.ID
 WHERE h.CREATED_AT >= TRUNC(SYSDATE) - 1
 GROUP BY h.ID, h.STATUS;
""")
    corpus.write(loader, f"""LOAD DATA
INFILE '{d.key}.csv'
APPEND
INTO TABLE {d.table('STAGING')}
FIELDS TERMINATED BY ','
(
  ID INTEGER EXTERNAL,
  HEADER_ID INTEGER EXTERNAL,
  QUANTITY INTEGER EXTERNAL,
  PRICE DECIMAL EXTERNAL,
  LOADED_AT SYSDATE
)
""")


# --------------------------------------------------------------------------- .NET API

def _dotnet_api(corpus: Corpus, d: Domain) -> None:
    file = f"{d.api}/{d.pascal}Api.cs"
    route = f"api/{d.plural}"
    controller, service, repository = f"{d.pascal}Controller", f"{d.pascal}Service", f"{d.pascal}Repository"

    def method(owner: str, name: str) -> tuple[str, str]:
        return ("method", f"{owner}.{name}")

    def api(verb: str, path: str) -> tuple[str, str]:
        return ("api", f"{verb} /{path}")

    for verb, path, action, target in (
        ("POST", route, "Create", "Create"), ("GET", f"{route}/{{id}}", "Read", "Read"),
        ("DELETE", f"{route}/{{id}}", "Cancel", "Cancel"), ("PUT", f"{route}/{{id}}/status", "UpdateStatus", "UpdateStatus"),
    ):
        corpus.edge("dotnet_controller_route", api(verb, path), "call", method(controller, action), file)
        corpus.edge("dotnet_service_call", method(controller, action), "call", method(service, target), file)
        corpus.edge("dotnet_service_call", method(service, target), "call", method(repository, target), file)
    corpus.edge("dotnet_minimal_api", api("GET", f"{route}/summary"), "call", method(service, "Summary"), file)
    corpus.edge("dotnet_service_call", method(service, "Summary"), "call", method(repository, "Summary"), file)
    corpus.edge("dotnet_stored_procedure", method(repository, "Create"), "call", ("procedure", f"{d.package}.CREATE_{d.upper}"), file)
    corpus.edge("dotnet_stored_procedure", method(repository, "Cancel"), "call", ("procedure", f"{d.package}.CANCEL_{d.upper}"), file)
    corpus.edge("dotnet_const_sql", method(repository, "Read"), "read", ("table", d.table("HEADER")), file)
    corpus.edge("dotnet_const_sql", method(repository, "Read"), "read", ("table", d.table("LINE")), file)
    corpus.edge("dotnet_verbatim_dml", method(repository, "UpdateStatus"), "write", ("table", d.table("HEADER")), file)
    corpus.edge("dotnet_stringbuilder_sql", method(repository, "Summary"), "read", ("table", d.table("HISTORY")), file)
    corpus.edge("dotnet_interpolated_table", method(repository, "Archive"), "write", ("table", d.table("HISTORY")), file)
    corpus.write(file, f"""using System;
using System.Data;
using System.Text;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Routing;

namespace Corpus.{d.pascal};

public record {d.pascal}Request(long CustomerId, string Status);
public record {d.pascal}Dto(long Id, string Status);

[ApiController]
[Route("{route}")]
public class {controller} : ControllerBase
{{
    private readonly {service} _service;

    public {controller}({service} service)
    {{
        _service = service;
    }}

    [HttpPost]
    public IActionResult Create([FromBody] {d.pascal}Request request) => Ok(_service.Create(request));

    [HttpGet("{{id}}")]
    public IActionResult Read(long id) => Ok(_service.Read(id));

    [HttpDelete("{{id}}")]
    public IActionResult Cancel(long id)
    {{
        _service.Cancel(id);
        return NoContent();
    }}

    [HttpPut("{{id}}/status")]
    public IActionResult UpdateStatus(long id, [FromBody] {d.pascal}Request request)
    {{
        _service.UpdateStatus(id, request.Status);
        return NoContent();
    }}
}}

public static class {d.pascal}Endpoints
{{
    public static void Map{d.pascal}(IEndpointRouteBuilder app)
    {{
        app.MapGet("/{route}/summary", ({service} service) => service.Summary());
    }}
}}

public class {service}
{{
    private readonly {repository} _repository;
    public {service}({repository} repository) => _repository = repository;
    public {d.pascal}Dto Create({d.pascal}Request request) => _repository.Create(request);
    public {d.pascal}Dto Read(long id) => _repository.Read(id);
    public void Cancel(long id) => _repository.Cancel(id);
    public void UpdateStatus(long id, string status) => _repository.UpdateStatus(id, status);
    public string Summary() => _repository.Summary();
}}

public class {repository}
{{
    public {d.pascal}Dto Create({d.pascal}Request request)
    {{
        using var command = new DbCommandStub
        {{
            CommandText = "{d.package}.CREATE_{d.upper}",
            CommandType = CommandType.StoredProcedure,
        }};
        return new {d.pascal}Dto(request.CustomerId, command.CommandText);
    }}

    public {d.pascal}Dto Read(long id)
    {{
        const string sql = "SELECT h.ID, h.STATUS FROM {d.table('HEADER')} h JOIN {d.table('LINE')} l ON l.HEADER_ID = h.ID WHERE h.ID = :id";
        return new {d.pascal}Dto(id, sql);
    }}

    public void Cancel(long id)
    {{
        using var command = new DbCommandStub
        {{
            CommandText = "{d.package}.CANCEL_{d.upper}",
            CommandType = CommandType.StoredProcedure,
        }};
        Console.WriteLine(command.CommandText + id);
    }}

    public void UpdateStatus(long id, string status)
    {{
        const string sql = @"UPDATE {d.table('HEADER')}
   SET STATUS = :status
 WHERE ID = :id";
        Console.WriteLine(sql + id + status);
    }}

    public string Summary()
    {{
        var sql = new StringBuilder();
        sql.Append("SELECT STATUS, COUNT(*) ");
        sql.Append("FROM {d.table('HISTORY')} ");
        sql.Append("GROUP BY STATUS");
        return sql.ToString();
    }}

    public void Archive(string suffix)
    {{
        var sql = $"INSERT INTO {d.upper}_{{suffix}} SELECT * FROM {d.table('HEADER')}";
        Console.WriteLine(sql);
    }}
}}

public sealed class DbCommandStub : IDisposable
{{
    public string CommandText {{ get; init; }} = "";
    public CommandType CommandType {{ get; init; }}
    public void Dispose() {{ }}
}}
""")


def _batch(corpus: Corpus) -> None:
    file = "batch/Program.cs"
    cases, methods, repositories = [], [], []
    for d in corpus.domains:
        mode = f"sync-{d.key}"
        cases.append(f"""        if (mode == "{mode}")
        {{
            return program.Sync{d.pascal}();
        }}""")
        methods.append(f"""    private int Sync{d.pascal}()
    {{
        _repository.Sync{d.pascal}();
        return 0;
    }}""")
        repositories.append(f"""    public void Sync{d.pascal}()
    {{
        using var command = new BatchCommand
        {{
            CommandText = "{d.package}.SYNC_{d.upper}",
            CommandType = CommandType.StoredProcedure,
        }};
        Console.WriteLine(command.CommandText);
    }}""")
        job = f"SYNC_{d.upper}"
        corpus.jobs.append({"jobnet_id": "NIGHTLY", "jobnet_name": "Nightly sync", "job_id": job,
                            "job_name": f"Sync {d.key}", "executable_name": "CorpusBatch.exe", "arguments": f"--mode {mode}"})
        corpus.edge("jobnet_job", ("job", job), "call", ("command_mode", mode), "jobnet.csv")
        corpus.edge("batch_mode_dispatch", ("command_mode", mode), "call", ("method", f"Program.Sync{d.pascal}"), file)
        corpus.edge("dotnet_service_call", ("method", f"Program.Sync{d.pascal}"), "call", ("method", f"BatchRepository.Sync{d.pascal}"), file)
        corpus.edge("dotnet_stored_procedure", ("method", f"BatchRepository.Sync{d.pascal}"), "call",
                    ("procedure", f"{d.package}.SYNC_{d.upper}"), file)
    for previous, current in zip(corpus.jobs, corpus.jobs[1:]):
        current["predecessor_job_id"] = previous["job_id"]
    corpus.write(file, f"""using System;
using System.Data;

namespace Corpus.Batch;

public sealed class Program
{{
    private readonly BatchRepository _repository = new();

    public static int Main(string[] args)
    {{
        var mode = args.Length > 1 && args[0] == "--mode" ? args[1] : "";
        var program = new Program();
{chr(10).join(cases)}
        return 2;
    }}

{chr(10).join(methods)}
}}

public sealed class BatchRepository
{{
{chr(10).join(repositories)}
}}

public sealed class BatchCommand : IDisposable
{{
    public string CommandText {{ get; init; }} = "";
    public CommandType CommandType {{ get; init; }}
    public void Dispose() {{ }}
}}
""")
    corpus.write("batch/CorpusBatch.csproj", """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net9.0</TargetFramework>
    <AssemblyName>CorpusBatch</AssemblyName>
  </PropertyGroup>
</Project>
""")


# --------------------------------------------------------------------------- Angular

def _angular(corpus: Corpus, d: Domain) -> None:
    file = f"web/src/app/{d.key}/{d.key}.ts"
    service, create_component, detail_component = f"{d.pascal}Service", f"Create{d.pascal}Component", f"{d.pascal}DetailComponent"
    route = f"api/{d.plural}"
    for path, component in ((f"{d.plural}/new", create_component), (f"{d.plural}/:id", detail_component)):
        corpus.edge("angular_route_screen", ("screen", path), "contains", ("component", component), "web/src/app/app.routes.ts")
    corpus.edge("angular_template_action", ("screen", f"{d.plural}/new"), "contains", ("ui_action", f"submit{d.pascal}"), file)
    corpus.edge("angular_template_action", ("screen", f"{d.plural}/:id"), "contains", ("ui_action", f"cancel{d.pascal}"), file)
    corpus.edge("angular_config_url", ("ui_action", f"submit{d.pascal}"), "api", ("api", f"POST /{route}"), file)
    corpus.edge("angular_template_url", ("ui_action", f"cancel{d.pascal}"), "api", ("api", f"DELETE /{route}/{{id}}"), file)
    corpus.edge("angular_config_url", ("angular_service", service), "api", ("api", f"POST /{route}"), file)
    corpus.edge("angular_template_url", ("angular_service", service), "api", ("api", f"GET /{route}/{{id}}"), file)
    corpus.edge("angular_template_url", ("angular_service", service), "api", ("api", f"DELETE /{route}/{{id}}"), file)
    corpus.edge("angular_request_method", ("angular_service", service), "api", ("api", f"PUT /{route}/{{id}}/status"), file)
    corpus.write(file, f"""import {{ Component, Injectable }} from '@angular/core';
import {{ HttpClient }} from '@angular/common/http';

@Injectable({{ providedIn: 'root' }})
export class {service} {{
  constructor(private readonly http: HttpClient, private readonly config: any) {{}}

  create(body: unknown) {{
    return this.http.post(this.config.create{d.pascal}, body);
  }}

  read(id: string) {{
    return this.http.get(`${{this.config.{d.key}Detail}}/${{id}}`);
  }}

  cancel(id: string) {{
    return this.http.delete(`${{this.config.{d.key}Detail}}/${{id}}`);
  }}

  updateStatus(id: string, status: string) {{
    const method = 'PUT';
    return this.http.request(method, `${{this.config.{d.key}Detail}}/${{id}}/status`, {{ body: {{ status }} }});
  }}
}}

@Component({{
  selector: 'app-create-{d.key}',
  template: `<button type="button" (click)="submit{d.pascal}()">Create {d.key}</button>`,
}})
export class {create_component} {{
  constructor(private readonly service: {service}) {{}}

  submit{d.pascal}() {{
    return this.service.create({{ customerId: 1 }});
  }}
}}

@Component({{
  selector: 'app-{d.key}-detail',
  template: `<button (click)="cancel{d.pascal}()">Cancel</button> <button (click)="hold{d.pascal}()">Hold</button>`,
}})
export class {detail_component} {{
  constructor(private readonly service: {service}) {{}}

  cancel{d.pascal}() {{
    return this.service.cancel('1');
  }}

  hold{d.pascal}() {{
    return this.service.updateStatus('1', 'HOLD');
  }}
}}
""")


def _angular_shell(corpus: Corpus) -> None:
    imports = "\n".join(
        f"import {{ Create{d.pascal}Component, {d.pascal}DetailComponent }} from './{d.key}/{d.key}';" for d in corpus.domains
    )
    routes = ",\n".join(
        f"  {{ path: '{d.plural}/new', component: Create{d.pascal}Component }},\n"
        f"  {{ path: '{d.plural}/:id', component: {d.pascal}DetailComponent }}"
        for d in corpus.domains
    )
    corpus.write("web/src/app/app.routes.ts", f"""import {{ Routes }} from '@angular/router';
{imports}

export const routes: Routes = [
{routes}
];
""")
    corpus.write("web/src/main.ts", "import { routes } from './app/app.routes';\nexport const appRoutes = routes;\n")
    config = {}
    for d in corpus.domains:
        config[f"create{d.pascal}"] = f"/{d.api}/api/{d.plural}"
        config[f"{d.key}Detail"] = f"/{d.api}/api/{d.plural}"
    corpus.write("web/src/assets/appconfig.json", json.dumps(config, indent=2) + "\n")
    corpus.write("web/angular.json", json.dumps({
        "version": 1, "defaultProject": "web",
        "projects": {"web": {"projectType": "application", "root": "", "sourceRoot": "src",
                             "architect": {"build": {"options": {"tsConfig": "tsconfig.app.json"}}}}},
    }, indent=2) + "\n")
    corpus.write("web/tsconfig.json", json.dumps({"compilerOptions": {"target": "ES2022", "module": "ES2022",
                                                                         "experimentalDecorators": True, "strict": False}}, indent=2) + "\n")
    corpus.write("web/tsconfig.app.json", json.dumps({"extends": "./tsconfig.json", "files": ["src/main.ts"],
                                                      "include": ["src/**/*.ts"]}, indent=2) + "\n")


# --------------------------------------------------------------------------- inputs, scenarios, files

def _inputs(corpus: Corpus, apis: list[str]) -> None:
    tables_rows = ["database_key,schema_name,object_name,object_type,status,comment"]
    column_rows = ["database_key,schema_name,object_name,column_name,ordinal_position,data_type,data_length,data_precision,"
                   "data_scale,nullable,default_value,primary_key,comment"]
    for table, columns in sorted(corpus.tables.items()):
        tables_rows.append(f"DB1,APP,{table},TABLE,VALID,{table.replace('_', ' ').title()}")
        for position, column in enumerate(columns, start=1):
            column_rows.append(f"DB1,APP,{table},{column},{position},NUMBER,,,,Y,,{'Y' if column == 'ID' else ''},")
    corpus.write("../catalog/incoming/database-tables__DB1.csv", "\n".join(tables_rows) + "\n")
    corpus.write("../catalog/incoming/database-columns__DB1.csv", "\n".join(column_rows) + "\n")
    dictionary = ["semantic_id,domain,locale,label,definition,aliases,status"]
    for d in corpus.domains:
        dictionary.append(f"semantic:{d.key}:create,{d.key},en,Create {d.key},Creates a new {d.key},CREATE_{d.upper}|submit{d.pascal},approved")
        dictionary.append(f"semantic:{d.key}:cancel,{d.key},en,Cancel {d.key},Cancels an open {d.key},CANCEL_{d.upper}|cancel{d.pascal},approved")
    corpus.write("../input-data/semantic-dictionary.csv", "\n".join(dictionary) + "\n")
    jobnet = ["jobnet_id,jobnet_name,job_id,job_name,executable_name,arguments,predecessor_job_id"]
    jobnet += [",".join([job["jobnet_id"], job["jobnet_name"], job["job_id"], job["job_name"], job["executable_name"],
                         job["arguments"], job.get("predecessor_job_id", "")]) for job in corpus.jobs]
    corpus.write("../input-data/jobnet.csv", "\n".join(jobnet) + "\n")
    corpus.write("../input-data/executable-mappings.csv",
                 "job_system,executable_name,executable_scope,canonical_executable_name,alias\n"
                 "batch,CorpusBatch.exe,batch,corpusbatch.exe,corpus-batch\n")
    owners = ["system,team,owner,contact,notes", "web,Team Web,Web lead,#web,", "batch,Team Batch,Batch lead,#batch,",
              "db,Team DB,DBA,#db,", "ops,Team DB,DBA,#db,", "DB1,Team DB,DBA,#db,"]
    owners += [f"{api},Team {api.split('-')[-1].title()},API lead,#{api}," for api in apis]
    corpus.write("../input-data/owners.csv", "\n".join(owners) + "\n")


def _scenarios(corpus: Corpus) -> None:
    """Change requests with their true impact, derived from the truth graph (so blind spots count)."""
    for d in corpus.domains[: min(4, len(corpus.domains))]:
        header, line, history = d.table("HEADER"), d.table("LINE"), d.table("HISTORY")
        corpus.write(f"../scenarios/{d.key}-status-column.yaml", _scenario_yaml(
            f"{d.key}-status-column",
            f"Đổi kiểu cột STATUS của bảng {header} sang VARCHAR2(20) và thêm trạng thái ON_HOLD cho {d.key}.",
            corpus, ("table", header), direction="in",
        ))
        corpus.write(f"../scenarios/{d.key}-cancel-rule.yaml", _scenario_yaml(
            f"{d.key}-cancel-rule",
            f"Khi hủy {d.key} thì không xoá dòng hàng nữa mà ghi lịch sử cho từng dòng.",
            corpus, ("procedure", f"{d.package}.CANCEL_{d.upper}"), direction="both",
            patch=[{
                "file": f"src/db/{d.key}_pkg.sql",
                "find": f"\t\tDELETE FROM {line} WHERE HEADER_ID = p_id;",
                "replace": f"\t\tINSERT INTO {history} (ID, HEADER_ID, STATUS, CHANGED_AT)\n"
                           f"\t\tSELECT {d.upper}_SEQ.NEXTVAL, HEADER_ID, 'LINE_CANCELLED', SYSDATE FROM {line} WHERE HEADER_ID = p_id;",
            }],
        ))


def _scenario_yaml(identifier: str, requirement: str, corpus: Corpus, target: tuple[str, str], *, direction: str,
                   patch: list[dict[str, str]] | None = None) -> str:
    affected = _reach(corpus.truth, target, direction)
    lines = [f"id: {identifier}", f"requirement: {json.dumps(requirement, ensure_ascii=False)}",
             f"target: {{kind: {target[0]}, name: {json.dumps(target[1])}}}", "truth:", "  must_change:",
             f"    - {{kind: {target[0]}, name: {json.dumps(target[1])}}}", "  affected:"]
    for item in affected:
        lines.append(f"    - {{kind: {item['kind']}, name: {json.dumps(item['name'])}, via: {item['pattern']}, "
                     f"expected: {item['expected']}}}")
    if patch:
        lines.append("patch:")
        for change in patch:
            lines += [f"  - file: {json.dumps(change['file'])}", f"    find: {json.dumps(change['find'])}",
                      f"    replace: {json.dumps(change['replace'])}"]
    return "\n".join(lines) + "\n"


_RANK = {"detectable": 0, "partial": 1, "undetectable": 2}


def _reach(truth: list[dict[str, Any]], start: tuple[str, str], direction: str) -> list[dict[str, Any]]:
    """What a change of ``start`` affects in the truth graph: everything that depends on it (callers, readers,
    writers and their callers up to the screens and jobs) and, for "both", what it reads, writes or calls.
    Each item keeps the weakest expectation on its path, so an impact through dynamic SQL counts as a blind spot."""
    def endpoint(edge: dict[str, Any], side: str) -> tuple[str, str]:
        return edge[side]["kind"], edge[side]["name"]

    found: dict[tuple[str, str], dict[str, Any]] = {}
    queue = [(start, "detectable")]
    while queue:
        node, weakest = queue.pop(0)
        for edge in truth:
            if endpoint(edge, "target") != node:
                continue
            other = endpoint(edge, "source")
            level = max(weakest, edge["expected"], key=_RANK.__getitem__)
            if other == start or (other in found and _RANK[found[other]["expected"]] <= _RANK[level]):
                continue
            found[other] = {"kind": other[0], "name": other[1], "expected": level, "pattern": edge["pattern"]}
            queue.append((other, level))
    if direction == "both":
        for edge in truth:
            if endpoint(edge, "source") == start and endpoint(edge, "target") not in found:
                other = endpoint(edge, "target")
                found[other] = {"kind": other[0], "name": other[1], "expected": edge["expected"], "pattern": edge["pattern"]}
    return sorted(found.values(), key=lambda item: (item["kind"], item["name"]))


def _oracle_dependencies(corpus: Corpus) -> str:
    """What Oracle's ALL_DEPENDENCIES would list for the PL/SQL: static references, per package/trigger.
    Dynamic SQL is invisible to Oracle as well, so those patterns are left out."""
    kinds = {"table": "TABLE", "sequence": "SEQUENCE", "procedure": "PACKAGE", "external": "TABLE"}
    rows = {"OWNER,NAME,TYPE,REFERENCED_OWNER,REFERENCED_NAME,REFERENCED_TYPE,REFERENCED_LINK_NAME"}
    for edge in corpus.truth:
        if edge["source"]["kind"] not in {"procedure", "trigger"} or edge["pattern"].startswith("plsql_dynamic"):
            continue
        kind = kinds.get(edge["target"]["kind"])
        if not kind:
            continue
        source = edge["source"]["name"]
        name, object_type = (source.split(".")[0], "PACKAGE BODY") if edge["source"]["kind"] == "procedure" else (source, "TRIGGER")
        referenced = edge["target"]["name"].split(".")[0] if kind == "PACKAGE" else edge["target"]["name"]
        link = "LEGACY_LINK" if edge["target"]["kind"] == "external" else ""
        if referenced != name:
            rows.add(f"APP,{name},{object_type},{'LEGACY' if link else 'APP'},{referenced},{kind},{link}")
    header = "OWNER,NAME,TYPE,REFERENCED_OWNER,REFERENCED_NAME,REFERENCED_TYPE,REFERENCED_LINK_NAME"
    return "\n".join([header, *sorted(rows - {header})]) + "\n"


def _write(corpus: Corpus, apis: list[str]) -> None:
    source_root = corpus.root / "src"
    for relative, text in corpus.files.items():
        path = (source_root / relative).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    truth = corpus.root / "truth"
    truth.mkdir(parents=True, exist_ok=True)
    (truth / "edges.jsonl").write_text("".join(json.dumps(edge, ensure_ascii=False) + "\n" for edge in corpus.truth), encoding="utf-8")
    (truth / "all_dependencies.csv").write_text(_oracle_dependencies(corpus), encoding="utf-8")
    (truth / "patterns.json").write_text(json.dumps(
        {name: {"expected": expected, "description": description} for name, (expected, description) in PATTERNS.items()},
        indent=2) + "\n", encoding="utf-8")
    sources = [
        {"name": "web", "type": "angular", "system": "corpus", "repository": "web", "folders": ["web/src/app"],
         "appConfig": "web/src/assets/appconfig.json"},
        *({"name": api, "type": "dotnet-api", "system": "corpus", "repository": api, "folders": [api], "database": "DB1",
           "schema": "APP", "sqlDialect": "oracle"} for api in apis),
        {"name": "batch", "type": "dotnet-batch", "system": "corpus", "repository": "batch", "folders": ["batch"],
         "database": "DB1", "schema": "APP", "sqlDialect": "oracle"},
        {"name": "db", "type": "oracle-plsql", "system": "corpus", "repository": "db", "folders": ["db"], "database": "DB1",
         "schema": "APP"},
        {"name": "ops", "type": "sql-files", "system": "corpus", "repository": "ops", "folders": ["ops"], "database": "DB1",
         "schema": "APP"},
    ]
    config = {"name": "corpus", "root": "./src", "output": "./out", "inputData": "./input-data",
              "catalog": {"folder": "./catalog", "strict": True}, "sources": sources}
    (corpus.root / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (corpus.root / "corpus.json").write_text(json.dumps({
        "size": corpus.size, "seed": corpus.seed, "domains": [d.key for d in corpus.domains],
        "truth_edges": len(corpus.truth), "files": len(corpus.files),
    }, indent=2) + "\n", encoding="utf-8")
