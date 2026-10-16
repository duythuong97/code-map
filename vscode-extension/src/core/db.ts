import * as fs from "node:fs";
import * as path from "node:path";

export type Row = Record<string, unknown>;
export type Param = string | number | null;

/** Read-only access to codemap-rag.sqlite. */
export interface GraphDb {
  readonly engine: "node:sqlite" | "sql.js";
  all(sql: string, params?: Param[]): Row[];
  close(): void;
}

/**
 * Opens the database with node:sqlite when the runtime has it (reads pages on
 * demand), otherwise with sql.js (loads the file into memory; the RAG file is
 * a fraction of graph.sqlite, e.g. 52 MB for a 3,600-file solution).
 */
export async function openGraphDb(file: string, wasmDir?: string): Promise<GraphDb> {
  if (!fs.existsSync(file)) throw new Error(`Graph database not found: ${file}`);
  const native = tryNodeSqlite(file);
  if (native) return native;
  return openSqlJs(file, wasmDir);
}

function tryNodeSqlite(file: string): GraphDb | undefined {
  let sqlite: { DatabaseSync: new (file: string, options?: object) => NodeSqliteDb };
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    sqlite = require("node:sqlite");
  } catch {
    return undefined;
  }
  try {
    const db = new sqlite.DatabaseSync(file, { readOnly: true });
    const statements = new Map<string, NodeSqliteStatement>();
    return {
      engine: "node:sqlite",
      all(sql, params = []) {
        let statement = statements.get(sql);
        if (!statement) {
          statement = db.prepare(sql);
          statements.set(sql, statement);
        }
        return statement.all(...params) as Row[];
      },
      close: () => db.close(),
    };
  } catch {
    return undefined;
  }
}

interface NodeSqliteStatement {
  all(...params: Param[]): unknown[];
}
interface NodeSqliteDb {
  prepare(sql: string): NodeSqliteStatement;
  close(): void;
}

async function openSqlJs(file: string, wasmDir?: string): Promise<GraphDb> {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const initSqlJs = require("sql.js") as typeof import("sql.js");
  const SQL = await initSqlJs({
    locateFile: (name: string) =>
      wasmDir ? path.join(wasmDir, name) : path.join(path.dirname(require.resolve("sql.js")), name),
  });
  const db = new SQL.Database(fs.readFileSync(file));
  return {
    engine: "sql.js",
    all(sql, params = []) {
      const statement = db.prepare(sql);
      try {
        statement.bind(params);
        const rows: Row[] = [];
        while (statement.step()) rows.push(statement.getAsObject() as Row);
        return rows;
      } finally {
        statement.free();
      }
    },
    close: () => db.close(),
  };
}

export function placeholders(count: number): string {
  return new Array(count).fill("?").join(",");
}
