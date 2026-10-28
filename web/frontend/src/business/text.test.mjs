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
assert.deepEqual(parseRoute("#/explore"), { view: "home", id: null });
assert.deepEqual(parseRoute("#/node/procedure%3ADB1%3APKG.P"), { view: "node", id: "procedure:DB1:PKG.P" });
assert.deepEqual(parseRoute(""), { view: "home", id: null });
assert.equal(routeHash("feature", "feature:screen:web:orders/new"), "#/feature/feature%3Ascreen%3Aweb%3Aorders%2Fnew");

// Expansion placement keeps existing nodes and puts new ones beside their neighbour without overlap.
const { placeNodes, defaultIntentKeys, baseIntentEdges, toggleBaseIntent, baseIntentChecked, connectedNodes } = await import("./graph-place.js");
const placed = placeNodes(new Map([["a", { x: 0, y: 0 }], ["b", { x: 460, y: 0 }]]), ["a", "b", "c", "d"], [
  { source_node_id: "a", target_node_id: "b" }, { source_node_id: "a", target_node_id: "c" }, { source_node_id: "d", target_node_id: "a" },
]);
assert.deepEqual(placed.get("a"), { x: 0, y: 0 });
assert.deepEqual(placed.get("c"), { x: 460, y: 150 });
assert.deepEqual(placed.get("d"), { x: -460, y: 0 });
// Defaults are the walks that explain the flow: E both ways, W/R where hops end in a table write/read; never "1".
const flowEdges = [
  { edge_id: "e1", source_node_id: "screen", target_node_id: "api", edge_type: "CALLS_API" },
  { edge_id: "e2", source_node_id: "api", target_node_id: "proc", edge_type: "CALLS" },
  { edge_id: "e3", source_node_id: "proc", target_node_id: "orders", edge_type: "INSERTS" },
  { edge_id: "e4", source_node_id: "proc", target_node_id: "prices", edge_type: "READS" },
];
const owned = baseIntentEdges(flowEdges);
const keys = defaultIntentKeys(flowEdges);
assert.ok(![...keys].some((key) => key.endsWith("|1")));
assert.deepEqual(owned.get("screen|out|E").path, ["e1", "e2", "e3", "e4"]);
assert.deepEqual(owned.get("screen|out|W").path, ["e1", "e2", "e3"]);
assert.deepEqual(owned.get("screen|out|R").path, ["e1", "e2", "e4"]);
assert.deepEqual(owned.get("orders|in|W").path, ["e3", "e2", "e1"]);
assert.deepEqual(owned.get("prices|in|R").path, ["e4", "e2", "e1"]);
assert.equal(owned.get("orders|in|R"), undefined);
assert.deepEqual(owned.get("proc|in|E").path, ["e2", "e1"]);
assert.deepEqual(owned.get("screen|out|W").core, ["e3"]);
assert.deepEqual(owned.get("screen|out|E").core, ["e1"]);
assert.deepEqual(owned.get("orders|in|W").core, ["e3"]);

// Unticking "proc out W" hides the write; both ends read as unticked, the table drops out, ticking either end restores it.
const onScreen = (hiddenIds, list = flowEdges, entry = "screen") => {
  const visible = list.filter((edge) => !hiddenIds.has(edge.edge_id));
  const nodes = connectedNodes([entry], visible);
  return { nodes: [...nodes].sort(), edges: new Set(visible.filter((edge) => nodes.has(edge.source_node_id)).map((edge) => edge.edge_id)) };
};
let hidden = toggleBaseIntent(new Set(), owned, "proc|out|W", false);
assert.deepEqual([...hidden], ["e3"]);
let view = onScreen(hidden);
assert.deepEqual(view.nodes, ["api", "prices", "proc", "screen"]);
assert.equal(baseIntentChecked(view.edges, owned, "orders|in|W"), false);
assert.equal(baseIntentChecked(view.edges, owned, "screen|out|W"), false);
assert.equal(baseIntentChecked(view.edges, owned, "screen|out|R"), true);
assert.equal(baseIntentChecked(view.edges, owned, "proc|out|E"), true);
hidden = toggleBaseIntent(hidden, owned, "screen|out|W", true);
assert.equal(hidden.size, 0);
// Unticking "api out E" drops everything below the API; their boxes read as unticked; "proc in E" brings it back.
hidden = toggleBaseIntent(new Set(), owned, "api|out|E", false);
view = onScreen(hidden);
assert.deepEqual(view.nodes, ["api", "screen"]);
assert.equal(baseIntentChecked(view.edges, owned, "screen|out|W"), false);
assert.equal(toggleBaseIntent(hidden, owned, "proc|in|E", true).size, 0);
// A node still connected to the entry another way stays.
const diamond = [...flowEdges, { edge_id: "e5", source_node_id: "screen", target_node_id: "proc", edge_type: "CALLS" }];
const diamondHidden = toggleBaseIntent(new Set(), baseIntentEdges(diamond), "api|out|E", false);
assert.deepEqual([...connectedNodes(["screen"], diamond.filter((edge) => !diamondHidden.has(edge.edge_id)))].sort(), ["api", "orders", "prices", "proc", "screen"]);

console.log("business text self-check passed");
