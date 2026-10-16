import { strict as assert } from "node:assert";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { after, before, describe, it } from "node:test";
import { GraphDb, openGraphDb } from "../src/core/db";
import { Retriever } from "../src/core/retriever";
import { compact, tokens } from "../src/core/text";

const repo = path.resolve(__dirname, "..", "..", "..");
const vectors = JSON.parse(fs.readFileSync(path.join(repo, "tests", "fixtures", "rag-token-vectors.json"), "utf8")) as {
  text: string;
  tokens: string[];
  compact: string;
}[];

describe("tokens", () => {
  for (const vector of vectors) {
    it(`matches the Python exporter for ${JSON.stringify(vector.text)}`, () => {
      assert.deepEqual(tokens(vector.text), vector.tokens);
      assert.equal(compact(vector.text), vector.compact);
    });
  }
});

// Built from the demo graph with the Python exporter, so both halves are tested together.
function demoDatabase(): string {
  const graph = path.join(repo, "output", "demo", "graph.sqlite");
  if (!fs.existsSync(graph)) throw new Error(`Run scripts/run_demo_pipeline.py first: ${graph} is missing`);
  const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "codemap-rag-")), "codemap-rag.sqlite");
  execFileSync(process.env.PYTHON ?? "python3", [
    "-m", "code_tree_exporter", "rag-export", "--graph", graph, "--input-data", path.join(repo, "input-data"), "--out", out,
  ], { cwd: repo, stdio: "pipe" });
  return out;
}

for (const engine of ["node:sqlite", "sql.js"] as const) {
  describe(`retriever (${engine})`, () => {
    let db: GraphDb;
    let retriever: Retriever;
    before(async () => {
      const file = demoDatabase();
      if (engine === "sql.js") {
        const realRequire = require("node:module").prototype.require;
        // Force the sql.js path by hiding node:sqlite for this open only.
        require("node:module").prototype.require = function (id: string) {
          if (id === "node:sqlite") throw new Error("hidden");
          return realRequire.apply(this, [id]);
        };
        try {
          db = await openGraphDb(file);
        } finally {
          require("node:module").prototype.require = realRequire;
        }
      } else {
        db = await openGraphDb(file);
      }
      assert.equal(db.engine, engine);
      retriever = new Retriever(db);
    });
    after(() => db.close());

    it("bridges a Vietnamese business term to the code that implements it", () => {
      const context = retriever.retrieve("Hủy đơn hàng xử lý ở đâu?");
      assert.equal(context.concepts[0].semanticId, "semantic:orders:cancel-order");
      const seedNames = context.seeds.map((node) => node.name);
      assert.ok(seedNames.includes("CancelOrder"), seedNames.join(","));
      assert.ok(seedNames.some((name) => name.startsWith("DELETE ")), seedNames.join(","));
      assert.match(context.text, /\[C1\] Cancel order \/ Hủy đơn hàng/);
    });

    it("answers who writes a table with evidence lines", () => {
      const context = retriever.retrieve("ORDER_HEADER", { mode: "impact" });
      assert.equal(context.seeds[0].name, "ORDER_HEADER");
      assert.match(context.text, /-WRITES_TO \(INSERT\)-> N1 @ order-db\//);
      assert.ok(context.references.some((reference) => reference.line != null));
    });

    it("follows a batch job from jobnet.csv into the code it runs", () => {
      const context = retriever.retrieve("Allocate orders job", { mode: "flow" });
      assert.ok(context.nodes.some((node) => node.type === "JOB" && node.name === "Allocate orders"));
      assert.match(context.text, /-INVOKES \(--mode allocate\)->/);
      assert.ok(context.nodes.some((node) => node.type === "COMMAND_MODE" && node.name === "allocate"));
    });

    it("finds the nodes declared in a file", () => {
      const context = retriever.retrieve("", { mode: "file", file: "order-api/OrderFlow.cs" });
      assert.ok(context.seeds.length > 0);
      assert.ok(context.seeds.every((node) => node.file === "order-api/OrderFlow.cs"));
    });

    it("lists business gaps", () => {
      const context = retriever.retrieve("", { mode: "gaps" });
      assert.match(context.text, /JOB_EXECUTABLE_NOT_FOUND/);
      assert.match(context.text, /PartnerExport\.exe/);
    });

    it("respects the context budget", () => {
      const context = retriever.retrieve("order", { maxContextChars: 2000 });
      assert.ok(context.text.length <= 2100);
      assert.ok(context.truncated);
    });
  });
}
