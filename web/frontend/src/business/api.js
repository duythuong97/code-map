import { api } from "../code-flow/api.js";

// Who reviews: sent with every write so approvals record a name (the backend refuses an approval without one).
export function currentUser() {
  try { return localStorage.getItem("bm.user") || ""; } catch { return ""; }
}

export function setCurrentUser(name) {
  try { localStorage.setItem("bm.user", name); } catch { /* private window: name lasts for this page only */ }
}

const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json", ...(currentUser() ? { "X-Code-Map-User": encodeURIComponent(currentUser()) } : {}) },
  body: JSON.stringify(body ?? {}),
});
const id = (value) => encodeURIComponent(value);

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
  reviewQueue: () => api("/business/review-queue"),
  kbStatus: () => api("/business/kb-status"),
  rules: (params = {}) => api(`/business/rules?${new URLSearchParams(params)}`),
  createRule: (body) => api("/business/rules", json("POST", body)),
  updateRule: (ruleId, body) => api(`/business/rules/${id(ruleId)}`, json("PATCH", body)),
  decisions: (params = {}) => api(`/business/decisions?${new URLSearchParams(params)}`),
  createDecision: (body) => api("/business/decisions", json("POST", body)),
  updateDecision: (decisionId, body) => api(`/business/decisions/${id(decisionId)}`, json("PATCH", body)),
  questions: (params = {}) => api(`/business/questions?${new URLSearchParams(params)}`),
  createQuestion: (body) => api("/business/questions", json("POST", body)),
  answerQuestion: (questionId, body) => api(`/business/questions/${id(questionId)}/answer`, json("POST", body)),
};
