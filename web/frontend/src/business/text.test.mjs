import assert from "node:assert/strict";
import {
  citedNames,
  flowParts,
  inlineTokens,
  nodeLookup,
  paragraphs,
  parseRoute,
  resolveCitation,
  reviewCounts,
  routeHash,
  swimlanes,
} from "./text.js";

assert.deepEqual(paragraphs("Một.\n\n  Hai.\n\n\n"), ["Một.", "Hai."]);
assert.deepEqual(inlineTokens("Gọi [[OrderController.Create]] rồi **ghi** `ORDER_HEADER`."), [
  { type: "text", value: "Gọi " },
  { type: "ref", value: "OrderController.Create" },
  { type: "text", value: " rồi " },
  { type: "bold", value: "ghi" },
  { type: "text", value: " " },
  { type: "code", value: "ORDER_HEADER" },
  { type: "text", value: "." },
]);
assert.deepEqual(citedNames("[[A]] và [[api: POST /x]]"), ["A", "api: POST /x"]);

const nodes = [
  { node_id: "m", name: "OrderController.Create", ref: "method: OrderController.Create", technical_name: "Create", node_type: "METHOD" },
  { node_id: "t", name: "ORDER_HEADER", ref: "table: ORDER_HEADER", technical_name: "ORDER_HEADER", node_type: "TABLE" },
  { node_id: "a", name: "POST /api/orders", ref: "api: POST /api/orders", technical_name: "POST /api/orders", node_type: "API_OPERATION" },
];
const lookup = nodeLookup(nodes);
assert.equal(resolveCitation(lookup, "OrderController.Create").node_id, "m");
assert.equal(resolveCitation(lookup, "table: ORDER_HEADER").node_id, "t");
assert.equal(resolveCitation(lookup, "procedure: ORDER_HEADER").node_id, "t");
assert.equal(resolveCitation(lookup, "ORDER_DISCOUNT"), null);

assert.deepEqual(reviewCounts(["approved", "ai", "edited", "ai", "none"]), { total: 5, approved: 1, edited: 1, ai: 2, none: 1 });
assert.deepEqual(flowParts({ summary: { status: "approved" }, steps: [{ id: "s1" }], edge_cases: { items: [] } }), [
  { key: "summary", status: "approved" },
  { key: "step:s1", status: "ai" },
]);

const lanes = swimlanes([{ nodes: ["a", "m"] }, { nodes: ["t"] }], new Map(nodes.map((node) => [node.node_id, node])));
assert.deepEqual(lanes.map((lane) => [lane.key, lane.cells.map((cell) => cell.map((node) => node.node_id))]), [
  ["api", [["a"], []]],
  ["logic", [["m"], []]],
  ["data", [[], ["t"]]],
]);

assert.deepEqual(parseRoute("#/flow/flow%3Aui-action%3Aweb%3Ax"), { view: "flow", id: "flow:ui-action:web:x" });
assert.deepEqual(parseRoute("#/explore"), { view: "explore", id: null });
assert.deepEqual(parseRoute(""), { view: "home", id: null });
assert.equal(routeHash("feature", "feature:screen:web:orders/new"), "#/feature/feature%3Ascreen%3Aweb%3Aorders%2Fnew");

console.log("business text self-check passed");
