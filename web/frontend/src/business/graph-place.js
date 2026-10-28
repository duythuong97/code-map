// Stable placement for the flow graph: nodes already on screen keep their position, and nodes an
// expansion adds are put next to the node they connect to (right for outgoing, left for incoming).
import { expansionIntentKey } from "../flow-state.js";

export const GAP_X = 380;
export const GAP_Y = 150;
const NODE_W = 320;
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
export function defaultIntentKeys(edges) {
  const keys = new Set();
  for (const edge of edges) {
    keys.add(expansionIntentKey(edge.source_node_id, "1", "out"));
    keys.add(expansionIntentKey(edge.target_node_id, "1", "in"));
  }
  return keys;
}

export function visibleBaseEdges(edges, suppressed) {
  return edges.filter((edge) => !suppressed.has(expansionIntentKey(edge.source_node_id, "1", "out"))
    && !suppressed.has(expansionIntentKey(edge.target_node_id, "1", "in")));
}
