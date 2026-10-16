// Code Map webview: draws the graph the extension sends, in left-to-right layers
// (entry points → APIs → application code → database code → data).
import cytoscape from "cytoscape";

const vscode = acquireVsCodeApi();
const LAYER_TITLES = ["Màn hình · Job", "API · Batch", "Code ứng dụng", "Code DB", "Dữ liệu"];
const COLUMN_WIDTH = 230;
const ROW_HEIGHT = 74;

const container = document.getElementById("map");
const info = document.getElementById("info");
const status = document.getElementById("status");
const search = document.getElementById("search");
const filters = [...document.querySelectorAll("[data-category]")];

const cy = cytoscape({
  container,
  wheelSensitivity: 0.25,
  minZoom: 0.1,
  maxZoom: 3,
  style: [
    {
      selector: "node",
      style: {
        shape: "round-rectangle",
        width: 190,
        height: 50,
        "background-color": "data(fill)",
        "border-width": 1,
        "border-color": "data(stroke)",
        label: "data(display)",
        color: "data(text)",
        "font-size": 11,
        "text-wrap": "wrap",
        "text-max-width": 176,
        "text-valign": "center",
        "text-halign": "center",
      },
    },
    { selector: "node.focus", style: { "border-width": 3 } },
    { selector: "node:selected", style: { "border-width": 3, "border-color": "#f2c94c" } },
    {
      selector: "edge",
      style: {
        width: 1.5,
        "curve-style": "bezier",
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.8,
        "line-color": "data(color)",
        "target-arrow-color": "data(color)",
        label: "data(display)",
        "font-size": 9,
        color: "data(color)",
        "text-background-color": "data(background)",
        "text-background-opacity": 0.85,
        "text-background-padding": 2,
        "text-rotation": "autorotate",
      },
    },
    { selector: "edge.structure", style: { "line-style": "dotted", width: 1 } },
    // Labels only where they stay readable: small maps, the hovered/selected edge, edges of the selected node.
    { selector: "edge.quiet", style: { label: "" } },
    { selector: "edge.hover, edge:selected, node:selected > edge", style: { label: "data(display)", "z-index": 10 } },
    { selector: ".hidden", style: { display: "none" } },
    { selector: ".dim", style: { opacity: 0.18 } },
  ],
});

const theme = () => {
  const css = getComputedStyle(document.body);
  const value = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  return {
    background: value("--vscode-editor-background", "#1e1e1e"),
    text: value("--vscode-editor-foreground", "#d4d4d4"),
    layers: ["#2f5d8a", "#2d6a5a", "#4b4f7a", "#7a5a2d", "#7a3b3b"],
  };
};

const EDGE_COLORS = { write: "#e8833a", read: "#4c9be8", call: "#9aa4b2", business: "#b07fe0", structure: "#6b7280", other: "#8b95a3" };

function nodeData(node) {
  const colors = theme();
  const business = (node.names || "").split("; ").find(Boolean) || "";
  return {
    ...node,
    id: String(node.id),
    display: `${node.type}\n${node.label}${business ? `\n${business.replace(/^\w+:/, "")}` : ""}`,
    fill: colors.layers[node.layer] || colors.layers[2],
    stroke: "#ffffff33",
    text: "#ffffff",
  };
}

function edgeData(edge) {
  const operation = edge.operation && edge.operation !== edge.type && edge.operation.length < 18 ? ` ${edge.operation}` : "";
  return {
    ...edge,
    id: `e${edge.id}`,
    source: String(edge.src),
    target: String(edge.dst),
    display: edge.category === "structure" ? "" : `${edge.type}${operation}`,
    color: EDGE_COLORS[edge.category] || EDGE_COLORS.other,
    background: theme().background,
  };
}

/** Column = layer, plus the depth of call chains inside the layer; rows ordered by neighbour position. */
function layout() {
  const nodes = cy.nodes(":visible");
  const depth = new Map(nodes.map((node) => [node.id(), 0]));
  for (let pass = 0; pass < 4; pass++) {
    cy.edges().forEach((edge) => {
      // A job DEPENDS_ON its predecessor: the predecessor runs first, so it goes left.
      const [source, target] = edge.data("type") === "DEPENDS_ON" ? [edge.target(), edge.source()] : [edge.source(), edge.target()];
      if (!depth.has(source.id()) || !depth.has(target.id())) return;
      if (source.data("layer") === target.data("layer") && edge.data("category") !== "structure") {
        depth.set(target.id(), Math.min(3, Math.max(depth.get(target.id()), depth.get(source.id()) + 1)));
      }
    });
  }
  const columns = new Map();
  nodes.forEach((node) => {
    const column = node.data("layer") * 4 + depth.get(node.id());
    if (!columns.has(column)) columns.set(column, []);
    columns.get(column).push(node);
  });
  const ordered = [...columns.keys()].sort((a, b) => a - b);
  const x = new Map(ordered.map((column, index) => [column, index * COLUMN_WIDTH]));
  const y = new Map();
  for (const column of ordered) {
    const members = columns.get(column);
    const barycentre = (node) => {
      const placed = node.neighborhood("node").filter((other) => y.has(other.id()));
      return placed.length ? placed.reduce((sum, other) => sum + y.get(other.id()), 0) / placed.length : Number.MAX_SAFE_INTEGER;
    };
    members.sort((a, b) => barycentre(a) - barycentre(b) || a.data("label").localeCompare(b.data("label")));
    members.forEach((node, row) => {
      y.set(node.id(), (row - (members.length - 1) / 2) * ROW_HEIGHT);
      node.position({ x: x.get(column), y: y.get(node.id()) });
    });
  }
  cy.fit(undefined, 40);
}

function applyFilters() {
  const hidden = new Set(filters.filter((input) => !input.checked).map((input) => input.dataset.category));
  cy.edges().forEach((edge) => edge.toggleClass("hidden", hidden.has(edge.data("category"))));
  // Nodes reached only through hidden edges (e.g. columns under "Cấu trúc") go too.
  cy.nodes().forEach((node) => {
    const visible = node.connectedEdges().some((edge) => !edge.hasClass("hidden"));
    node.toggleClass("hidden", !visible && !node.hasClass("focus") && node.connectedEdges().length > 0);
  });
  cy.edges().forEach((edge) => edge.toggleClass("quiet", cy.edges(":visible").length > 16));
}

function show(graph, merge) {
  if (!merge) cy.elements().remove();
  cy.add(graph.nodes.filter((node) => cy.getElementById(String(node.id)).empty()).map((node) => ({ group: "nodes", data: nodeData(node) })));
  cy.add(graph.edges.filter((edge) => cy.getElementById(`e${edge.id}`).empty()).map((edge) => ({ group: "edges", data: edgeData(edge) })));
  for (const id of graph.focus || []) cy.getElementById(String(id)).addClass("focus");
  applyFilters();
  layout();
  status.textContent = `${cy.nodes().length} node · ${cy.edges().length} quan hệ${graph.truncated ? " · đã cắt bớt, nhấp đúp để mở rộng" : ""}`;
}

function describe(node) {
  const data = node.data();
  const where = data.file ? `${data.file}${data.line ? `:${data.line}` : ""}` : "";
  info.textContent = [data.type, data.label, data.names, where].filter(Boolean).join("  ·  ");
}

cy.on("tap", "node", (event) => {
  describe(event.target);
  if (event.target.data("file")) vscode.postMessage({ type: "open", file: event.target.data("file"), line: event.target.data("line") });
});
cy.on("tap", "edge", (event) => {
  const data = event.target.data();
  info.textContent = `${data.type}${data.operation ? ` (${data.operation})` : ""}${data.file ? `  ·  ${data.file}:${data.line || ""}` : ""}`;
  if (data.file) vscode.postMessage({ type: "open", file: data.file, line: data.line });
});
cy.on("mouseover", "edge", (event) => event.target.addClass("hover"));
cy.on("mouseout", "edge", (event) => event.target.removeClass("hover"));
cy.on("dbltap", "node", (event) => vscode.postMessage({ type: "expand", id: Number(event.target.id()) }));

const selected = () => cy.$("node:selected").first();
document.getElementById("impact").addEventListener("click", () => selected().nonempty() && vscode.postMessage({ type: "focus", id: Number(selected().id()), mode: "impact" }));
document.getElementById("flow").addEventListener("click", () => selected().nonempty() && vscode.postMessage({ type: "focus", id: Number(selected().id()), mode: "flow" }));
document.getElementById("ask").addEventListener("click", () => selected().nonempty() && vscode.postMessage({ type: "ask", name: selected().data("label") }));
document.getElementById("fit").addEventListener("click", () => cy.fit(undefined, 40));
filters.forEach((input) =>
  input.addEventListener("change", () => {
    applyFilters();
    layout();
  }),
);
search.addEventListener("input", () => {
  const text = search.value.trim().toLowerCase();
  cy.nodes().forEach((node) => node.toggleClass("dim", !!text && !`${node.data("label")} ${node.data("names")}`.toLowerCase().includes(text)));
});

// Legend: node colour = layer.
document.getElementById("columns").innerHTML = LAYER_TITLES.map(
  (title, layer) => `<span><i class="layer" style="background:${theme().layers[layer]}"></i>${title}</span>`,
).join("");

window.addEventListener("message", (event) => {
  const message = event.data;
  if (message.type === "graph") show(message.graph, message.merge);
  if (message.type === "title") document.getElementById("title").textContent = message.title;
});
vscode.postMessage({ type: "ready" });
