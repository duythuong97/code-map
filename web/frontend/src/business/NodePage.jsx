import React, { useEffect, useState } from "react";
import { ArrowLeft, FileText, Network } from "lucide-react";
import { businessApi } from "./api.js";
import { api } from "../code-flow/api.js";
import DetailPanel from "../code-flow/DetailPanel.jsx";
import { FlowGraph } from "./FlowView.jsx";
import { typeLabel } from "./text.js";

const START = [["1", "in"], ["1", "out"]];

// Graph from any code node (what the old explorer did), with the explorer's detail panel beside it.
export default function NodePage({ nodeId, onOpenItem, onOpenNode, notify }) {
  const [page, setPage] = useState(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(nodeId);
  const [detail, setDetail] = useState(null);
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let live = true;
    setPage(null); setError(""); setSelected(nodeId); setHistory([]);
    businessApi.node(nodeId).then((body) => { if (live) setPage(body); }).catch((reason) => { if (live) setError(reason.message); });
    return () => { live = false; };
  }, [nodeId]);

  useEffect(() => {
    if (!selected) return undefined;
    const controller = new AbortController();
    setLoading(true);
    api(`/detail?target_id=${encodeURIComponent(selected)}&target_type=NODE&locale=vi&relations=0`, { signal: controller.signal })
      .then((body) => setDetail(body))
      .catch((reason) => { if (reason.name !== "AbortError") setDetail({ error: reason.message }); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [selected]);

  function select(id) {
    if (!id || id === selected) return;
    setHistory((current) => [...current, selected]);
    setSelected(id);
  }

  if (error) return <div className="bm-pad"><p className="bm-error">{error}</p></div>;
  if (!page) return <div className="bm-pad"><p className="bm-muted">Đang tải…</p></div>;
  const node = page.node;
  const shown = detail?.node || { node_id: selected };
  return <div className="bm-node-page">
    <section className="bm-stage">
      <div className="bm-stage-bar">
        <span className="bm-kind">{typeLabel(node.node_type)}</span>
        <strong className="bm-node-title" title={node.node_id}>{node.name}</strong>
        <span className="bm-muted small">Tick 1 / W / R / E hai bên node để mở rộng</span>
      </div>
      <FlowGraph key={node.node_id} nodes={[node]} edges={[]} entry={node.node_id} initialExpand={START}
        selected={selected} onNodeClick={select} notify={notify} />
    </section>
    <aside className="bm-node-side">
      {page.flows.length ? <div className="bm-node-flows"><span className="bm-label">Thuộc luồng</span>
        {page.flows.map((flow) => <button key={flow.id} type="button" className="bm-node-flow" onClick={() => onOpenItem({ kind: "flow", id: flow.id })}>
          <FileText size={13} /><span><b>{flow.name}</b><small>{flow.path}</small></span></button>)}
      </div> : <p className="bm-muted small bm-node-none">Node này chưa thuộc luồng nào đã tách.</p>}
      {selected !== node.node_id ? <div className="bm-node-actions">
        <button type="button" className="bm-btn sm" onClick={() => onOpenNode(selected)}><Network size={14} /> Mở graph từ node này</button>
      </div> : null}
      <div className="bm-node-detail">
        {history.length ? <button type="button" className="bm-btn sm" onClick={() => { setSelected(history.at(-1)); setHistory((current) => current.slice(0, -1)); }}><ArrowLeft size={14} /> Quay lại</button> : null}
        <DetailPanel selected={{ ...shown, targetType: "NODE" }} detail={detail?.error ? null : detail} canGoBack={false}
          onSelectNode={select} localized={{}} loading={loading} error={detail?.error || ""} />
      </div>
    </aside>
  </div>;
}
