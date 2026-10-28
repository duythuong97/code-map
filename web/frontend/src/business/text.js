// Pure helpers for business documents. No React here so they can be checked with node.

// A document paragraph is split into text runs, **bold**, `code` and [[node]] citations.
const INLINE = /\*\*(.+?)\*\*|`([^`]+)`|\[\[(.+?)\]\]/g;

export function paragraphs(text = "") {
  return String(text).split(/\n\s*\n/).map((paragraph) => paragraph.trim()).filter(Boolean);
}

export function inlineTokens(paragraph = "") {
  const tokens = [];
  let last = 0;
  for (const match of String(paragraph).matchAll(INLINE)) {
    if (match.index > last) tokens.push({ type: "text", value: paragraph.slice(last, match.index) });
    if (match[1] !== undefined) tokens.push({ type: "bold", value: match[1] });
    else if (match[2] !== undefined) tokens.push({ type: "code", value: match[2] });
    else tokens.push({ type: "ref", value: match[3] });
    last = match.index + match[0].length;
  }
  if (last < paragraph.length) tokens.push({ type: "text", value: paragraph.slice(last) });
  return tokens;
}

export function citedNames(text = "") {
  return [...String(text).matchAll(/\[\[(.+?)\]\]/g)].map((match) => match[1]);
}

// Names a citation can use for a node: its document name, its "type: name" ref and its technical name.
export function nodeLookup(nodes = []) {
  const lookup = new Map();
  for (const node of nodes) {
    for (const key of [node.ref, node.name, node.technical_name]) {
      if (key && !lookup.has(key)) lookup.set(key, node);
    }
  }
  return lookup;
}

export function resolveCitation(lookup, name) {
  const text = String(name || "").trim();
  if (lookup.has(text)) return lookup.get(text);
  const bare = /^[a-z_]+:\s/.test(text) ? text.split(":").slice(1).join(":").trim() : text;
  return lookup.get(bare) || null;
}

// Review progress of a flow or of everything under a tree item.
export function reviewCounts(parts = []) {
  const counts = { total: parts.length, approved: 0, edited: 0, ai: 0, none: 0 };
  for (const part of parts) counts[part in counts ? part : "ai"] += 1;
  return counts;
}

export const PART_LABELS = { ai: "AI viết · chưa duyệt", edited: "Đã sửa", approved: "Đã duyệt" };
export const RULE_LABELS = { ok: "Khớp code", warn: "Cần xác nhận", bad: "Lệch với code" };

export function flowParts(flow) {
  if (!flow) return [];
  const parts = [{ key: "summary", status: flow.summary?.status || "ai" }];
  for (const step of flow.steps || []) parts.push({ key: `step:${step.id}`, status: step.status || "ai" });
  if (flow.edge_cases?.items?.length) parts.push({ key: "edge_cases", status: flow.edge_cases.status || "ai" });
  return parts;
}

// Swimlanes are the systems a flow crosses (repository for code, database for DB objects),
// in the order the flow first reaches them.
// The system a step runs in: the one most of its nodes belong to (ties go to its first node),
// or the system the graph split it on.
export function stepSystem(step, nodesById = new Map()) {
  const counts = new Map();
  let first = "";
  for (const id of step.nodes || []) {
    const system = nodesById.get(id)?.system;
    if (!system) continue;
    first ||= system;
    counts.set(system, (counts.get(system) || 0) + 1);
  }
  let best = first;
  counts.forEach((count, system) => { if (count > (counts.get(best) || 0)) best = system; });
  return best || step.system || "Khác";
}

// Swimlanes: one lane per system, in the order the flow first reaches it; each step sits in its system's lane.
export function swimlanes(steps = [], nodesById = new Map()) {
  const placement = steps.map((step) => stepSystem(step, nodesById));
  const order = [];
  placement.forEach((system) => { if (!order.includes(system)) order.push(system); });
  const lanes = order.map((system) => ({
    key: system,
    label: system,
    types: [...new Set(steps.flatMap((step, index) => (placement[index] === system ? step.nodes || [] : []))
      .map((id) => nodesById.get(id)?.node_type).filter(Boolean))],
  }));
  return { lanes, placement };
}

export function typeLabel(nodeType = "") {
  return nodeType.replace(/_/g, " ");
}

// Hash routes: #/feature/<id>, #/flow/<id>, #/node/<node_id>. The old #/explore lands on the home page.
export function parseRoute(hash = "") {
  const match = String(hash).replace(/^#/, "").match(/^\/(feature|flow|node)(?:\/(.*))?$/);
  if (!match) return { view: "home", id: null };
  return { view: match[1], id: match[2] ? decodeURIComponent(match[2]) : null };
}

export function routeHash(view, id) {
  return id ? `#/${view}/${encodeURIComponent(id)}` : "#/";
}
