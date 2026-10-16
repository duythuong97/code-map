export function mergeGroup(groups, key, payload) {
  return { ...groups, [key]: payload };
}

export function removeGroup(groups, key) {
  const next = { ...groups };
  delete next[key];
  return next;
}

export function groupLoadState(states, key) {
  return states?.[key] || { loading: false, error: "", truncated: false };
}

export function setGroupLoadState(states, key, patch) {
  return {
    ...states,
    [key]: {
      ...groupLoadState(states, key),
      ...patch,
    },
  };
}

export function removeGroupLoadState(states, key) {
  const next = { ...states };
  delete next[key];
  return next;
}

export function expansionIntentKey(nodeId, mode, direction = "both") {
  return `${nodeId}|${direction}|${mode}`;
}

export function mergeExpansionIntent(intents, node, mode, direction = "both") {
  if (!node?.node_id) return intents || {};
  return {
    ...(intents || {}),
    [expansionIntentKey(node.node_id, mode, direction)]: { node, mode, direction },
  };
}

export function removeExpansionIntent(intents, nodeId, mode, direction = "both") {
  const next = { ...(intents || {}) };
  delete next[expansionIntentKey(nodeId, mode, direction)];
  return next;
}

export function materializeGroups(root, groups) {
  const nodes = new Map(root ? [[root.node_id, root]] : []);
  const edges = new Map();
  const nodeOwners = new Map(root ? [[root.node_id, new Set(["root"])]] : []);
  const owners = new Map();
  Object.entries(groups).forEach(([key, group]) => {
    (group.nodes || []).forEach((node) => {
      nodes.set(node.node_id, { ...nodes.get(node.node_id), ...node });
      if (!nodeOwners.has(node.node_id)) nodeOwners.set(node.node_id, new Set());
      nodeOwners.get(node.node_id).add(key);
    });
    (group.edges || []).forEach((edge) => {
      edges.set(edge.edge_id, edge);
      if (!owners.has(edge.edge_id)) owners.set(edge.edge_id, new Set());
      owners.get(edge.edge_id).add(key);
    });
  });
  const connected = new Set(root ? [root.node_id] : []);
  edges.forEach((edge) => {
    connected.add(edge.source_node_id);
    connected.add(edge.target_node_id);
  });
  return {
    nodes: [...nodes.values()].filter((node) => connected.has(node.node_id)).map((node) => ({ ...node, loadedBy: [...(nodeOwners.get(node.node_id) || [])] })),
    edges: [...edges.values()].map((edge) => ({ ...edge, owners: [...owners.get(edge.edge_id)] })),
  };
}

export function isExternalNode(node) {
  return ["EXTERNAL_SYSTEM", "EXTERNAL_API_OPERATION", "EXTERNAL_DATABASE_OBJECT", "UNRESOLVED_REFERENCE"].includes(node.node_type);
}
