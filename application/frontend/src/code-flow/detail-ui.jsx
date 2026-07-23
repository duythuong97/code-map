import React from "react";
import { formatValue, identityText, isEmpty } from "./format.js";

export function Badge({ children, tone = "neutral" }) {
  return <span className={`detail-badge ${tone}`}>{children}</span>;
}

export function EntityIdentity({ node, localized, compact = false }) {
  return <span className={`entity-identity ${compact ? "compact" : ""}`}>{identityText(node, localized)}</span>;
}

export function EmptyState({ children }) {
  return <p className="detail-empty">{children}</p>;
}

export function FieldGrid({ rows }) {
  const visible = (rows || []).filter(([, value]) => !isEmpty(value));
  if (!visible.length) return <EmptyState>No additional fields available.</EmptyState>;
  return <dl className="detail-field-grid">
    {visible.map(([label, value]) => <React.Fragment key={label}>
      <dt>{label}</dt><dd>{formatValue(value)}</dd>
    </React.Fragment>)}
  </dl>;
}

export function DetailDisclosure({ title, children }) {
  return <details className="detail-disclosure">
    <summary>{title}</summary>
    <div>{children}</div>
  </details>;
}

export function MetricCards({ items }) {
  return <div className="detail-metrics">
    {items.filter((item) => !isEmpty(item.value)).map((item) => <div className="detail-metric" key={item.label}>
      <span>{item.label}</span>
      <strong>{item.value}</strong>
    </div>)}
  </div>;
}

export function DetailHeader({ selected, detail, localized }) {
  const isEdge = selected?.targetType === "EDGE" || selected?.edge_id;
  const node = detail?.node || (!isEdge ? selected : null);
  const edge = detail?.edge || (isEdge ? selected : null);
  const sourceNode = detail?.source || { node_id: edge?.source_node_id };
  const targetNode = detail?.target || { node_id: edge?.target_node_id };
  return <header className="detail-header">
    <div className="detail-title-row">
      <Badge tone={isEdge ? "edge" : "node"}>{isEdge ? "EDGE" : node?.node_type}</Badge>
      <h3>{isEdge ? edge?.edge_type || "Edge" : <EntityIdentity node={node} localized={localized} />}</h3>
      {isEdge ? <div className="bilingual-endpoints"><EntityIdentity node={sourceNode} localized={localized} compact /><span>→</span><EntityIdentity node={targetNode} localized={localized} compact /></div> : null}
    </div>
    {isEdge && edge?.graph_layer ? <div className="detail-header-badges"><Badge>{edge.graph_layer}</Badge></div> : null}
  </header>;
}

export function TabBar({ tabs, activeTab, onChange }) {
  return <div className="detail-tabs" role="tablist" aria-label="Detail sections">
    {tabs.map((tab) => <button key={tab.id} type="button" role="tab" aria-selected={activeTab === tab.id} className={activeTab === tab.id ? "active" : ""} onClick={() => onChange(tab.id)}>
      {tab.label}{tab.count ? <span>{tab.count}</span> : null}
    </button>)}
  </div>;
}

export function DetailLinkButton({ children, onClick, title }) {
  return <button className="detail-link-button" type="button" onClick={onClick} title={title}>{children}</button>;
}
