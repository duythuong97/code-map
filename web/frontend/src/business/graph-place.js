// Stable placement for the flow graph: nodes already on screen keep their position, and nodes an
// expansion adds are put next to the node they connect to (right for outgoing, left for incoming).
import { expansionIntentKey } from "../flow-state.js";

export const GAP_X = 460;
export const GAP_Y = 150;
const NODE_W = 420;
const NODE_H = 120;

export function placeNodes(positions, nodeIds, edges) {
  const placed = new Map(positions);
  const links = new Map();
  for (const edge of edges) {
    if (!links.has(edge.target_node_id)) links.set(edge.target_node_id, []);
    if (!links.has(edge.source_node_id)) links.set(edge.source_node_id, []);
    links.get(edge.target_node_id).push([edge.source_node_id, 1]);
    links.get(edge.source_node_id).push([edge.target_node_id, -1]);
  }
  const taken = (point) => [...placed.values()].some((other) => Math.abs(other.x - point.x) < NODE_W && Math.abs(other.y - point.y) < NODE_H);
  const pending = [...new Set(nodeIds)].filter((id) => !placed.has(id));
  let progress = true;
  while (pending.length && progress) {
    progress = false;
    for (let index = 0; index < pending.length; index += 1) {
      const id = pending[index];
      const anchor = (links.get(id) || []).find(([other]) => placed.has(other));
      if (!anchor) continue;
      const [neighbour, side] = anchor;
      const origin = placed.get(neighbour);
      const point = { x: origin.x + side * GAP_X, y: origin.y };
      while (taken(point)) point.y += GAP_Y;
      placed.set(id, point);
      pending.splice(index, 1);
      index -= 1;
      progress = true;
    }
  }
  const bottom = Math.max(0, ...[...placed.values()].map((point) => point.y));
  pending.forEach((id, index) => placed.set(id, { x: 0, y: bottom + (index + 1) * GAP_Y }));
  return placed;
}

// Edge kinds the explorer's W/R walks use: intermediate hops, then a write or read of a table.
const WRITES = new Set(["INSERTS", "UPDATES", "DELETES", "MERGES", "WRITES"]);
const READS = new Set(["READS", "REMOTE_READS"]);
const HOPS = new Set(["CALLS_API", "STARTS", "CALLS", "HANDLED_BY", "RESOLVES_TO", "TRIGGERS"]);

// The flow is a multi-hop walk from its entry, so its edges are what the E/W/R boxes would have shown, not "1".
// Each default box has a `path` (the flow edges its walk covers) and a `core` (the edges that make it true):
//   out E: path = everything downstream, core = the node's own outgoing edges     in E: the same upstream
//   out W/R: path = hop chains ending in a write/read, core = those writes/reads
//   in W/R (tables): path = writes/reads into the table and the hops before them, core = those writes/reads
// Unticking hides the core (what hangs only off it then drops out), ticking shows the whole path again, and a box
// reads as ticked while one of its core edges is on screen. One edge sits in several boxes, so both ends agree.
export function baseIntentEdges(edges) {
  const out = new Map();
  const into = new Map();
  for (const edge of edges) {
    if (!out.has(edge.source_node_id)) out.set(edge.source_node_id, []);
    if (!into.has(edge.target_node_id)) into.set(edge.target_node_id, []);
    out.get(edge.source_node_id).push(edge);
    into.get(edge.target_node_id).push(edge);
  }
  const walk = (start, links, next) => {
    const seen = new Set();
    const ids = [];
    const queue = [start];
    const visited = new Set([start]);
    while (queue.length) {
      for (const edge of links.get(queue.shift()) || []) {
        if (seen.has(edge.edge_id)) continue;
        seen.add(edge.edge_id); ids.push(edge.edge_id);
        const other = next(edge);
        if (!visited.has(other)) { visited.add(other); queue.push(other); }
      }
    }
    return ids;
  };
  // Edges on hop-chains from a node that end in a terminal of the given kind (cycles count as dead ends).
  const pathsOut = (node, terminals, visiting = new Set()) => {
    if (visiting.has(node)) return [];
    visiting.add(node);
    const ids = [];
    for (const edge of out.get(node) || []) {
      if (terminals.has(edge.edge_type)) ids.push(edge.edge_id);
      else if (HOPS.has(edge.edge_type)) {
        const rest = pathsOut(edge.target_node_id, terminals, visiting);
        if (rest.length) ids.push(edge.edge_id, ...rest);
      }
    }
    visiting.delete(node);
    return ids;
  };
  const hopsInto = (node, visiting = new Set()) => {
    if (visiting.has(node)) return [];
    visiting.add(node);
    const ids = [];
    for (const edge of into.get(node) || []) {
      if (HOPS.has(edge.edge_type)) ids.push(edge.edge_id, ...hopsInto(edge.source_node_id, visiting));
    }
    visiting.delete(node);
    return ids;
  };
  const owned = new Map();
  const types = new Map(edges.map((edge) => [edge.edge_id, edge.edge_type]));
  const put = (node, mode, direction, path, core) => {
    if (core.length) owned.set(expansionIntentKey(node, mode, direction), { path: [...new Set(path)], core: [...new Set(core)] });
  };
  for (const node of new Set([...out.keys(), ...into.keys()])) {
    put(node, "E", "out", walk(node, out, (edge) => edge.target_node_id), (out.get(node) || []).map((edge) => edge.edge_id));
    put(node, "E", "in", walk(node, into, (edge) => edge.source_node_id), (into.get(node) || []).map((edge) => edge.edge_id));
    for (const [mode, terminals] of [["W", WRITES], ["R", READS]]) {
      const forward = pathsOut(node, terminals);
      put(node, mode, "out", forward, forward.filter((id) => terminals.has(types.get(id))));
      const writes = (into.get(node) || []).filter((edge) => terminals.has(edge.edge_type));
      put(node, mode, "in", writes.flatMap((edge) => [edge.edge_id, ...hopsInto(edge.source_node_id)]), writes.map((edge) => edge.edge_id));
    }
  }
  return owned;
}

export function defaultIntentKeys(edges) {
  return new Set(baseIntentEdges(edges).keys());
}

export function toggleBaseIntent(hidden, owned, key, checked) {
  const next = new Set(hidden);
  const box = owned.get(key);
  if (box) (checked ? box.path : box.core).forEach((edgeId) => (checked ? next.delete(edgeId) : next.add(edgeId)));
  return next;
}

// `shown` holds the edge ids actually on screen.
export function baseIntentChecked(shown, owned, key) {
  return (owned.get(key)?.core || []).some((edgeId) => shown.has(edgeId));
}

// What stays on screen: every node still connected to a seed through a shown edge. Unticking a hop therefore
// drops whatever hung off it, the way removing an expansion did in the explorer, while the side that still
// leads back to the entry stays.
export function connectedNodes(seeds, edges) {
  const links = new Map();
  for (const edge of edges) {
    if (!links.has(edge.source_node_id)) links.set(edge.source_node_id, []);
    if (!links.has(edge.target_node_id)) links.set(edge.target_node_id, []);
    links.get(edge.source_node_id).push(edge.target_node_id);
    links.get(edge.target_node_id).push(edge.source_node_id);
  }
  const seen = new Set(seeds);
  const queue = [...seen];
  while (queue.length) {
    const id = queue.shift();
    for (const next of links.get(id) || []) {
      if (!seen.has(next)) { seen.add(next); queue.push(next); }
    }
  }
  return seen;
}

// One seed per connected part of the flow graph (the entry for its own part), so unticking can drop what hangs
// off a hop without dropping parts that were never joined to the entry, such as a hand-added node.
export function componentSeeds(nodeIds, edges, entry) {
  const seeds = [];
  const covered = new Set();
  for (const id of [entry, ...nodeIds]) {
    if (!id || covered.has(id) || !nodeIds.includes(id)) continue;
    seeds.push(id);
    connectedNodes([id], edges).forEach((member) => covered.add(member));
  }
  return seeds;
}
