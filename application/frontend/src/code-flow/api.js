export const api = async (path, options) => {
  const response = await fetch(`${window.CODE_MAP_CONFIG?.urlPrefix || ""}/api/graph${path}`, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || `Request failed (${response.status})`);
  return body;
};

export const EVIDENCE_PAGE_SIZE = 50;

export const emptyEvidencePage = () => ({
  items: [],
  cursor: null,
  truncated: false,
  count: 0,
  evidence_count: 0,
  evidence_truncated: false,
  evidence_scope: "direct",
  direct_evidence_count: 0,
  trace_evidence_count: 0,
});
