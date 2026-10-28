import { api } from "../code-flow/api.js";

const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body ?? {}),
});
const id = (value) => encodeURIComponent(value).replace(/%2F/g, "/");

export const businessApi = {
  tree: () => api("/business/tree"),
  item: (itemId) => api(`/business/items/${id(itemId)}`),
  updateItem: (itemId, body) => api(`/business/items/${id(itemId)}`, json("PATCH", body)),
  flow: (flowId) => api(`/business/flows/${id(flowId)}`),
  updatePart: (flowId, part, body) => api(`/business/flows/${id(flowId)}/parts/${encodeURIComponent(part)}`, json("PATCH", body)),
  answer: (flowId, questionId, answer) => api(`/business/flows/${id(flowId)}/questions/${encodeURIComponent(questionId)}/answer`, json("POST", { answer })),
  exportPack: (featureId, flags) => api(`/business/features/${id(featureId)}/ai-export?${new URLSearchParams(flags)}`, { method: "POST" }),
  checkImport: (featureId, text) => api(`/business/features/${id(featureId)}/ai-import/check`, json("POST", { text })),
  applyImport: (featureId, text) => api(`/business/features/${id(featureId)}/ai-import`, json("POST", { text })),
  node: (nodeId) => api(`/business/nodes/${id(nodeId)}`),
  search: (query, signal) => api(`/business/search?q=${encodeURIComponent(query)}`, { signal }),
};
