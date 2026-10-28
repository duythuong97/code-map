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
  const pending = nodeIds.filter((id) => !placed.has(id));
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

// The flow's own edges count as the 1-hop expansions that are already on: out of each source, into each target.
// Each such intent owns the base edges it covers, so one edge belongs to two intents (one per end).
export function baseIntentEdges(edges) {
  const owned = new Map();
  const add = (key, edgeId) => {
    if (!owned.has(key)) owned.set(key, []);
    owned.get(key).push(edgeId);
  };
  for (const edge of edges) {
    add(expansionIntentKey(edge.source_node_id, "1", "out"), edge.edge_id);
    add(expansionIntentKey(edge.target_node_id, "1", "in"), edge.edge_id);
  }
  return owned;
}

export function defaultIntentKeys(edges) {
  return new Set(baseIntentEdges(edges).keys());
}

// Unticking a base intent hides its edges, ticking shows them again. A box reads as ticked while any of its
// edges is still shown, so both ends of an edge always agree.
export function toggleBaseIntent(hidden, owned, key, checked) {
  const next = new Set(hidden);
  (owned.get(key) || []).forEach((edgeId) => (checked ? next.delete(edgeId) : next.add(edgeId)));
  return next;
}

export function baseIntentChecked(hidden, owned, key) {
  return (owned.get(key) || []).some((edgeId) => !hidden.has(edgeId));
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
