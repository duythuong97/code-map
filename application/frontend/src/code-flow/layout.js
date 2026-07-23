import { LAYOUT_COLUMN_GAP, LAYOUT_MARGIN, LAYOUT_ROW_GAP, nodeTypeRank } from "./constants.js";
import { displayName } from "./format.js";

export function stableNodeCompare(localized = {}) {
  return (left, right) => {
    const typeDelta = (nodeTypeRank[left.node_type] || 999) - (nodeTypeRank[right.node_type] || 999);
    if (typeDelta) return typeDelta;
    return `${displayName(left, localized)} ${left.node_id}`.localeCompare(`${displayName(right, localized)} ${right.node_id}`);
  };
}

export function stableEdgeCompare(left, right) {
  return `${left.source_node_id}|${left.target_node_id}|${left.edge_type}|${left.edge_id}`.localeCompare(`${right.source_node_id}|${right.target_node_id}|${right.edge_type}|${right.edge_id}`);
}

export function buildAutoLayout(nodes, edges, anchorId, localized = {}) {
  const orderedNodes = [...(nodes || [])].sort(stableNodeCompare(localized));
  if (!orderedNodes.length) return [];
  const nodeIds = new Set(orderedNodes.map((node) => node.node_id));
  const orderedEdges = [...(edges || [])].filter((edge) => nodeIds.has(edge.source_node_id) && nodeIds.has(edge.target_node_id)).sort(stableEdgeCompare);
  const outgoing = new Map(orderedNodes.map((node) => [node.node_id, []]));
  const incoming = new Map(orderedNodes.map((node) => [node.node_id, []]));
  orderedEdges.forEach((edge) => {
    outgoing.get(edge.source_node_id)?.push(edge.target_node_id);
    incoming.get(edge.target_node_id)?.push(edge.source_node_id);
  });
  const anchor = anchorId && nodeIds.has(anchorId) ? anchorId : orderedNodes[0].node_id;
  const walk = (links) => {
    const distance = new Map([[anchor, 0]]);
    const queue = [anchor];
    for (let index = 0; index < queue.length; index += 1) {
      const current = queue[index];
      const nextDistance = distance.get(current) + 1;
      [...(links.get(current) || [])].sort().forEach((next) => {
        if (!distance.has(next)) {
          distance.set(next, nextDistance);
          queue.push(next);
        }
      });
    }
    return distance;
  };
  const outgoingDistance = walk(outgoing);
  const incomingDistance = walk(incoming);
  const rankByNode = new Map();
  orderedNodes.forEach((node) => {
    const out = outgoingDistance.get(node.node_id);
    const inc = incomingDistance.get(node.node_id);
    if (node.node_id === anchor) rankByNode.set(node.node_id, 0);
    else if (out !== undefined && inc !== undefined) rankByNode.set(node.node_id, out <= inc ? out : -inc);
    else if (out !== undefined) rankByNode.set(node.node_id, out);
    else if (inc !== undefined) rankByNode.set(node.node_id, -inc);
    else rankByNode.set(node.node_id, 0);
  });
  const layers = new Map();
  orderedNodes.forEach((node) => {
    const rank = rankByNode.get(node.node_id) || 0;
    if (!layers.has(rank)) layers.set(rank, []);
    layers.get(rank).push(node);
  });
  const positioned = [];
  [...layers.entries()].sort(([left], [right]) => left - right).forEach(([, layer], layerIndex) => {
    layer.sort(stableNodeCompare(localized));
    const verticalOffset = ((layer.length - 1) * LAYOUT_ROW_GAP) / 2;
    layer.forEach((node, rowIndex) => positioned.push({
      ...node,
      position: {
        x: layerIndex * LAYOUT_COLUMN_GAP,
        y: rowIndex * LAYOUT_ROW_GAP - verticalOffset,
      },
    }));
  });
  const minX = Math.min(...positioned.map((node) => node.position.x));
  const minY = Math.min(...positioned.map((node) => node.position.y));
  return positioned.map((node) => ({
    ...node,
    position: {
      x: node.position.x - minX + LAYOUT_MARGIN,
      y: node.position.y - minY + LAYOUT_MARGIN,
    },
  }));
}
