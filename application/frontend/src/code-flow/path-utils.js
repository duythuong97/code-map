import { actorTypeLabels, operationDetails } from "./constants.js";
import { displayName, formatLabel, groupBy } from "./format.js";

export function operationInfo(operation) {
  return operationDetails[operation] || { label: formatLabel(operation || "Flow"), title: formatLabel(operation || "Flow"), tone: "neutral", action: "uses", impactAction: "uses this data" };
}

export function operationRank(operation) {
  if (operation === "W") return 0;
  if (operation === "R") return 1;
  return 2;
}

export function pathDepth(path) {
  return path?.edge_path?.length || 0;
}

export function pathEvidenceCount(path) {
  return path?.evidence_ids?.length || 0;
}

export function pathEndpoint(path, mode) {
  if (mode === "flow") {
    return {
      id: path.table_node_id,
      name: path.table_name || path.table_technical_name,
      technicalName: path.table_technical_name,
      type: path.table_type,
      database: path.table_database_key,
    };
  }
  return {
    id: path.actor_node_id,
    name: path.actor_name || path.actor_technical_name,
    technicalName: path.actor_technical_name,
    type: path.actor_type,
    database: path.actor_database_key,
    repository: path.actor_repository_key,
  };
}

export function pathEndpointNode(path, mode) {
  const endpoint = pathEndpoint(path, mode);
  return { node_id: endpoint.id, default_display_name: endpoint.name, technical_name: endpoint.technicalName, node_type: endpoint.type };
}

export function pathEndpointName(path, mode, localized = {}) {
  const endpoint = pathEndpoint(path, mode);
  return displayName(pathEndpointNode(path, mode), localized) || endpoint.id || endpoint.name || "Unknown";
}

export function dedupeFlowPaths(paths = [], mode = "flow", localized = {}) {
  const unique = new Map();
  paths.forEach((path) => {
    const endpoint = pathEndpoint(path, mode);
    const key = `${path.operation || ""}|${endpoint.id || endpoint.name || path.path_id}`;
    const previous = unique.get(key);
    if (!previous || pathEvidenceCount(path) > pathEvidenceCount(previous) || (pathEvidenceCount(path) === pathEvidenceCount(previous) && pathDepth(path) < pathDepth(previous))) {
      unique.set(key, path);
    }
  });
  return [...unique.values()].sort((left, right) => {
    const rankDelta = operationRank(left.operation) - operationRank(right.operation);
    if (rankDelta) return rankDelta;
    return pathEndpointName(left, mode, localized).localeCompare(pathEndpointName(right, mode, localized));
  });
}

export function relationSummary(rows = []) {
  const counts = groupBy(rows, (row) => row.edge_type);
  return Object.entries(counts)
    .sort(([left], [right]) => left.localeCompare(right))
    .slice(0, 4)
    .map(([edgeType, items]) => `${formatLabel(edgeType)} ${items.length}`)
    .join(" · ");
}

export function endpointListText(paths, mode, localized, emptyText) {
  const names = dedupeFlowPaths(paths, mode, localized).map((path) => pathEndpointName(path, mode, localized));
  if (!names.length) return emptyText;
  if (names.length <= 3) return names.join(", ");
  return `${names.slice(0, 3).join(", ")} +${names.length - 3} more`;
}

export function typeLabel(endpoint) {
  return actorTypeLabels[endpoint.type] || formatLabel(endpoint.type || "Node");
}

export function terminalTraceEdge(path) {
  const edgePath = path?.edge_path || [];
  return edgePath.length ? edgePath[edgePath.length - 1] : "";
}

export function materializedFlowEdge(path) {
  return path?.flow_edge_id || "";
}

export function pathDrilldownEdge(path) {
  return materializedFlowEdge(path) || terminalTraceEdge(path);
}
