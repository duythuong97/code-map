import { importantPropertyKeys } from "./constants.js";

export const parseJson = (value, fallback = {}) => {
  try {
    return JSON.parse(value || "");
  } catch {
    return fallback;
  }
};

export const props = (row) => parseJson(row?.properties_json, {});

export function formatLabel(value = "") {
  return String(value)
    .replace(/[_:-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

export function normalizeSearchText(value = "") {
  return String(value).trim().toLowerCase();
}

export function prioritizeSearchResults(term, rows = []) {
  const normalized = normalizeSearchText(term);
  return [...rows].sort((left, right) => {
    const leftFields = [left.localized_display_name, left.default_display_name, left.technical_name, left.qualified_name, left.node_id].map(normalizeSearchText);
    const rightFields = [right.localized_display_name, right.default_display_name, right.technical_name, right.qualified_name, right.node_id].map(normalizeSearchText);
    const leftExact = leftFields.some((field) => field === normalized) ? 0 : 1;
    const rightExact = rightFields.some((field) => field === normalized) ? 0 : 1;
    if (leftExact !== rightExact) return leftExact - rightExact;
    return displayName(left).localeCompare(displayName(right));
  });
}

export function localizedName(node, localized = {}, language = "en") {
  if (!node) return "";
  const values = language === "ja"
    ? [localized[node.node_id]?.ja?.name]
    : [localized[node.node_id]?.en?.name, node.default_display_name, node.technical_name, node.node_id];
  return values.map((value) => friendlyDisplayText(value, node.node_id)).find(Boolean) || "";
}

export function displayName(node, localized = {}) {
  return localizedName(node, localized, "ja") || localizedName(node, localized, "en");
}

export function bilingualNames(node, localized = {}) {
  const ja = localizedName(node, localized, "ja");
  const en = localizedName(node, localized, "en");
  return { ja, en, hasBoth: Boolean(ja && en && ja !== en) };
}

export function identityParts(node, localized = {}) {
  if (!node) return { code: "—", ja: "", en: "" };
  const code = friendlyDisplayText(node.technical_name, node.node_id) || friendlyDisplayText(node.node_id) || "—";
  const ja = friendlyDisplayText(localized[node.node_id]?.ja?.name || node.localized_display_name, node.node_id);
  const rawEn = localized[node.node_id]?.en?.name || node.default_display_name;
  const en = rawEn && rawEn !== code && rawEn !== ja ? friendlyDisplayText(rawEn, node.node_id) : "";
  return { code, ja, en };
}

export function identityText(node, localized = {}) {
  const { code, ja, en } = identityParts(node, localized);
  const names = [ja, en].filter(Boolean).join(" - ");
  return names ? `${code} (${names})` : code;
}

export function humanizeInternalId(value = "") {
  const text = String(value || "");
  if (!text) return "";
  const parts = text.split(":").filter(Boolean);
  return parts.length > 1 ? parts[parts.length - 1] : text;
}

export function looksLikeInternalId(value = "", nodeId = "") {
  const text = String(value || "").trim();
  if (!text || !text.includes(":")) return false;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) return false;
  return text === nodeId || /^[a-z_][a-z0-9_]*:.+:.+/.test(text);
}

export function friendlyDisplayText(value = "", nodeId = "") {
  const text = String(value || "").trim();
  if (!text) return "";
  return looksLikeInternalId(text, nodeId) ? humanizeInternalId(text) : text;
}

export function secondaryNodeText(node, localized = {}) {
  if (!node) return "";
  const primary = displayName(node, localized);
  return [props(node).route, props(node).path, props(node).source_path]
    .map((value) => friendlyDisplayText(value, node.node_id))
    .find((value) => !isEmpty(value) && value !== primary) || "";
}

const hiddenPropertyKeys = new Set(["edge_id", "edge_path", "node_id", "path_id", "source_id", "source_node_id", "target_id", "target_node_id"]);

export function visiblePropertyEntries(data = {}) {
  return Object.entries(data).filter(([key, value]) => !hiddenPropertyKeys.has(key) && !isEmpty(value));
}

export function localizedDescription(node, localized = {}, language = "en") {
  if (!node?.node_id) return "";
  return localized[node.node_id]?.[language]?.description || "";
}

export function collectLocalizationIds(...values) {
  const ids = new Set();
  const visit = (value) => {
    if (!value) return;
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (typeof value !== "object") return;
    Object.entries(value).forEach(([key, child]) => {
      if ((key === "node_id" || key.endsWith("_node_id")) && typeof child === "string" && child) {
        ids.add(child);
      } else if (child && typeof child === "object") {
        visit(child);
      }
    });
  };
  values.forEach(visit);
  return [...ids].sort();
}

export function isEmpty(value) {
  return value === undefined || value === null || value === "" || (Array.isArray(value) && value.length === 0);
}

export function formatValue(value) {
  if (value === true) return "Yes";
  if (value === false) return "No";
  if (Array.isArray(value)) return value.map((item) => typeof item === "string" ? friendlyDisplayText(item) : formatValue(item)).join(", ");
  if (typeof value === "object" && value !== null) return JSON.stringify(value);
  return friendlyDisplayText(value ?? "");
}

export function shortId(value = "") {
  if (!value) return "";
  return value.length > 96 ? `${value.slice(0, 45)}…${value.slice(-36)}` : value;
}

export function groupBy(items, keyFn) {
  return (items || []).reduce((acc, item) => {
    const key = keyFn(item) || "Other";
    acc[key] = acc[key] || [];
    acc[key].push(item);
    return acc;
  }, {});
}

export function propertyRows(row, preferred = importantPropertyKeys) {
  const data = props(row);
  const preferredRows = preferred
    .filter((key) => !hiddenPropertyKeys.has(key) && !isEmpty(data[key]))
    .map((key) => [formatLabel(key), data[key]]);
  const remainingRows = visiblePropertyEntries(data)
    .filter(([key, value]) => !preferred.includes(key) && !isEmpty(value))
    .slice(0, 24)
    .map(([key, value]) => [formatLabel(key), value]);
  return [...preferredRows, ...remainingRows];
}

export function confidencePercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${Math.round(number * 100)}%` : "—";
}
