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
  stepSystem,
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
  { node_id: "m", name: "OrderController.Create", ref: "method: OrderController.Create", technical_name: "Create", node_type: "METHOD", system: "order-api" },
  { node_id: "t", name: "ORDER_HEADER", ref: "table: ORDER_HEADER", technical_name: "ORDER_HEADER", node_type: "TABLE", system: "ORDER_DB" },
  { node_id: "a", name: "POST /api/orders", ref: "api: POST /api/orders", technical_name: "POST /api/orders", node_type: "API_OPERATION", system: "order-api" },
  { node_id: "u", name: "CreateOrderComponent.submitOrder", ref: "ui_action: CreateOrderComponent.submitOrder", technical_name: "submitOrder", node_type: "UI_ACTION", system: "customer-web" },
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

const byId = new Map(nodes.map((node) => [node.node_id, node]));
const lanes = swimlanes([{ nodes: ["u"] }, { nodes: ["a", "m"] }, { nodes: ["t"] }, { nodes: ["m"] }], byId);
assert.deepEqual(lanes.lanes.map((lane) => lane.key), ["customer-web", "order-api", "ORDER_DB"]);
assert.deepEqual(lanes.placement, ["customer-web", "order-api", "ORDER_DB", "order-api"]);
assert.equal(stepSystem({ nodes: ["a", "t", "m"] }, byId), "order-api");
assert.equal(stepSystem({ nodes: [], system: "Jobnet" }, byId), "Jobnet");

assert.deepEqual(parseRoute("#/flow/flow%3Aui-action%3Aweb%3Ax"), { view: "flow", id: "flow:ui-action:web:x" });
assert.deepEqual(parseRoute("#/explore"), { view: "explore", id: null });
assert.deepEqual(parseRoute(""), { view: "home", id: null });
assert.equal(routeHash("feature", "feature:screen:web:orders/new"), "#/feature/feature%3Ascreen%3Aweb%3Aorders%2Fnew");

// Expansion placement keeps existing nodes and puts new ones beside their neighbour without overlap.
const { placeNodes, defaultIntentKeys, visibleBaseEdges } = await import("./graph-place.js");
const placed = placeNodes(new Map([["a", { x: 0, y: 0 }], ["b", { x: 460, y: 0 }]]), ["a", "b", "c", "d"], [
  { source_node_id: "a", target_node_id: "b" }, { source_node_id: "a", target_node_id: "c" }, { source_node_id: "d", target_node_id: "a" },
]);
assert.deepEqual(placed.get("a"), { x: 0, y: 0 });
assert.deepEqual(placed.get("c"), { x: 460, y: 150 });
assert.deepEqual(placed.get("d"), { x: -460, y: 0 });
const base = [{ edge_id: "e", source_node_id: "a", target_node_id: "b" }];
const keys = defaultIntentKeys(base);
assert.ok(keys.has("a|out|1") && keys.has("b|in|1"));
assert.deepEqual(visibleBaseEdges(base, new Set(["a|out|1"])), []);

console.log("business text self-check passed");
