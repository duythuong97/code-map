import { DetailHeader, EmptyState } from "./detail-ui.jsx";
import { BusinessSemantic, ColumnsTab, NodeOverview } from "./detail-tabs.jsx";

export default function DetailPanel({ selected, detail, canGoBack, onBack, onSelectNode, localized, loading, error }) {
  if (!selected) return <section className="code-flow-detail-shell"><h3>Detail</h3><EmptyState>Select a node.</EmptyState></section>;
  if (loading && !detail) return <section className="code-flow-detail-shell"><EmptyState>Loading detail…</EmptyState></section>;
  if (error && !detail) return <section className="code-flow-detail-shell"><EmptyState>{error}</EmptyState></section>;

  const node = detail?.node || selected;
  return <section className="code-flow-detail-shell">
    {canGoBack ? <button type="button" className="detail-back" onClick={onBack} aria-label="Back to previous detail">← Back</button> : null}
    <DetailHeader selected={selected} detail={detail} localized={localized} />
    <div className="detail-tab-body">
      <BusinessSemantic node={node} selected={detail?.business_semantics || []} />
      {node.node_type === "TABLE"
        ? <ColumnsTab columns={detail?.columns || []} localized={localized} />
        : <NodeOverview node={node} nodes={detail?.semantic_tree_nodes || {}} businessSemantics={detail?.semantic_tree_business_semantics || {}} localized={localized} onSelectNode={onSelectNode} />}
    </div>
  </section>;
}
