import React, { useEffect, useState } from "react";
import { api } from "./api.js";
import { Badge, DetailLinkButton, EmptyState, EntityIdentity } from "./detail-ui.jsx";
import {
  bilingualNames,
  formatLabel,
  friendlyDisplayText,
  localizedDescription,
  props,
} from "./format.js";

export function BusinessSemantic({ node, selected = [] }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [author, setAuthor] = useState("");
  const [choice, setChoice] = useState("");
  const [saved, setSaved] = useState(selected);
  const [editing, setEditing] = useState(false);
  const selectedSemanticId = selected[0]?.semantic_id || "";

  useEffect(() => {
    setSaved(selected);
    setItems([]);
    setChoice("");
    setError("");
    setEditing(false);
  }, [node?.node_id, selectedSemanticId]);

  async function loadSuggestions() {
    if (!node?.node_id || items.length) return;
    setLoading(true);
    setError("");
    try {
      const payload = await api(`/business-semantic-suggestions?target_id=${encodeURIComponent(node.node_id)}&locale=vi&limit=3`);
      setSaved(payload.selected || []);
      setItems(payload.items || []);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setLoading(false);
    }
  }

  async function choose() {
    if (!choice) {
      setError("Chọn một semantic.");
      return;
    }
    if (!author.trim()) {
      setError("Nhập tên người xác nhận.");
      return;
    }
    setError("");
    try {
      const payload = await api(`/business-semantic-mappings/${encodeURIComponent(node.node_id)}?locale=vi`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ semantic_id: choice, author_name: author.trim() }),
      });
      setSaved(payload.items || []);
      setItems([]);
      setChoice("");
      setEditing(false);
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  const semantic = saved[0];
  return (
    <section className="detail-subcard business-semantic" aria-labelledby="business-semantic-title">
      <div className="business-semantic-heading">
        <h4 id="business-semantic-title">Business meaning</h4>
        {semantic ? <Badge>confirmed</Badge> : <Badge>unconfirmed</Badge>}
      </div>
      {semantic && !editing ? (
        <button type="button" className="business-semantic-value" title={`${semantic.definition} · Click để đổi`} aria-label={`${semantic.label}. ${semantic.definition}. Click để đổi.`} onClick={() => setEditing(true)}>
          <span>{semantic.label}</span><i aria-hidden="true">i</i>
        </button>
      ) : (
        <details className="business-semantic-picker" open={editing || undefined} onToggle={(event) => {
          if (event.currentTarget.open) loadSuggestions();
          else if (semantic) setEditing(false);
        }}>
          <summary>{semantic ? "Đổi business meaning" : "ⓘ Xem semantic được gợi ý"}</summary>
          {loading ? <p role="status">Đang tìm semantic…</p> : null}
          {!loading && !items.length && !error ? <p>Không tìm thấy semantic phù hợp. Giữ nguyên tên kỹ thuật.</p> : null}
          {items.length ? <>
            <ul>{items.map((item) => <li key={item.semantic_id} className={choice === item.semantic_id ? "selected" : ""}>
              <label className="business-semantic-option-heading">
                <input type="radio" name="business-semantic" value={item.semantic_id} checked={choice === item.semantic_id} onChange={(event) => setChoice(event.target.value)} />
                <strong>{item.label}</strong>
                <span className="business-semantic-score">{Math.round(item.confidence * 100)}% phù hợp</span>
              </label>
              <p>{item.definition}</p>
              <small>{item.reasons.join(" · ")}</small>
            </li>)}</ul>
            <div className="business-semantic-auth">
              <label>
                <span>Người xác nhận</span>
                <input value={author} onChange={(event) => setAuthor(event.target.value)} placeholder="Nhập họ tên" />
              </label>
              <button type="button" onClick={choose}>Xác nhận</button>
            </div>
          </> : null}
          {error ? <p className="business-semantic-error" role="alert">{error}</p> : null}
        </details>
      )}
    </section>
  );
}

function SemanticInfo({ semantic }) {
  if (!semantic) return null;
  return <details className="semantic-tree-info">
    <summary aria-label={`Xem semantic: ${semantic.label}`}>i</summary>
    <span className="semantic-tree-popover" role="tooltip">
      <strong>{semantic.label}</strong>
      <span>{semantic.definition}</span>
      <small>Xác nhận bởi {semantic.author_name}</small>
    </span>
  </details>;
}

function SemanticBranch({ item, nodes, businessSemantics, localized, onSelectNode }) {
  const semantic = businessSemantics[item.ref_node_id];
  const referencedNode = nodes[item.ref_node_id];
  return (
    <li>
      <div className="semantic-tree-row">
        <Badge>{formatLabel(item.type)}</Badge>
        {item.ref_node_id ? (
          <DetailLinkButton onClick={() => onSelectNode?.(item.ref_node_id)} title={`Open ${item.label}`}>
            <EntityIdentity node={referencedNode || { node_id: item.ref_node_id }} localized={localized} compact />
          </DetailLinkButton>
        ) : (
          <span>{item.label}</span>
        )}
        <SemanticInfo semantic={semantic} />
        {item.action ? <code>{item.action}</code> : null}
      </div>
      {item.children?.length ? (
        <ul>{item.children.map((child, index) => <SemanticBranch key={`${child.type}:${child.ref_node_id || child.label}:${index}`} item={child} nodes={nodes} businessSemantics={businessSemantics} localized={localized} onSelectNode={onSelectNode} />)}</ul>
      ) : null}
    </li>
  );
}

function SemanticSection({ title, items, nodes, businessSemantics, localized, onSelectNode }) {
  if (!items?.length) return null;
  return (
    <section>
      <h5>{title}</h5>
      <ul>{items.map((item, index) => <SemanticBranch key={`${title}:${item.name || item.label}:${index}`} item={{ type: item.type || title, label: item.label || [item.name, item.type].filter(Boolean).join(": "), ...item }} nodes={nodes} businessSemantics={businessSemantics} localized={localized} onSelectNode={onSelectNode} />)}</ul>
    </section>
  );
}

export function NodeOverview({ node, nodes = {}, businessSemantics = {}, localized = {}, onSelectNode }) {
  if (!node) return <EmptyState>Node detail is unavailable.</EmptyState>;
  const semanticTree = props(node).semantic_tree;
  const hasV2Content = semanticTree?.version === 2 && ["parameters", "steps", "outputs", "exceptions", "analysis_notes"].some(key => semanticTree[key]?.length);
  if (!hasV2Content && !semanticTree?.children?.length)
    return <EmptyState>No semantic tree available for this node.</EmptyState>;
  return (
    <section className="detail-subcard semantic-tree">
      <h4>Semantic tree</h4>
      {semanticTree.version === 2 ? (
        <>
          <div className="semantic-tree-row"><Badge>operation</Badge><EntityIdentity node={node} localized={localized} compact /><SemanticInfo semantic={businessSemantics[node.node_id]} /></div>
          {semanticTree.summary ? <p>{semanticTree.summary}</p> : null}
          <SemanticSection title="Parameters" items={semanticTree.parameters} nodes={nodes} businessSemantics={businessSemantics} localized={localized} onSelectNode={onSelectNode} />
          <SemanticSection title="Steps" items={semanticTree.steps} nodes={nodes} businessSemantics={businessSemantics} localized={localized} onSelectNode={onSelectNode} />
          <SemanticSection title="Outputs" items={semanticTree.outputs} nodes={nodes} businessSemantics={businessSemantics} localized={localized} onSelectNode={onSelectNode} />
          <SemanticSection title="Exceptions" items={semanticTree.exceptions} nodes={nodes} businessSemantics={businessSemantics} localized={localized} onSelectNode={onSelectNode} />
          <SemanticSection title="Analysis notes" items={semanticTree.analysis_notes} nodes={nodes} businessSemantics={businessSemantics} localized={localized} onSelectNode={onSelectNode} />
        </>
      ) : <ul><SemanticBranch item={semanticTree} nodes={nodes} businessSemantics={businessSemantics} localized={localized} onSelectNode={onSelectNode} /></ul>}
    </section>
  );
}

export function ColumnsTab({ columns, localized }) {
  if (!columns?.length)
    return <EmptyState>No columns found for this table.</EmptyState>;
  return (
    <section className="columns-detail">
      <div className="detail-table-wrap">
        <table className="detail-table">
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">Column code</th>
              <th scope="col">日本語名</th>
              <th scope="col">English name</th>
              <th scope="col">Data type</th>
              <th scope="col">Nullable</th>
              <th scope="col">Related table</th>
              <th scope="col">Note</th>
            </tr>
          </thead>
          <tbody>
            {columns.map((column, index) => {
              const p = props(column);
              const names = bilingualNames(column, localized);
              const columnCode = friendlyDisplayText(
                p.column_code || column.technical_name,
                column.node_id,
              );
              const descriptionJa = localizedDescription(
                column,
                localized,
                "ja",
              );
              const descriptionEn = localizedDescription(
                column,
                localized,
                "en",
              );
              const nullable =
                p.nullable == null
                  ? null
                  : ![false, "false", "N", "NO", 0].includes(p.nullable);
              return (
                <tr key={column.node_id}>
                  <td>
                    <span className="column-position">
                      {p.ordinal_position || index + 1}
                    </span>
                  </td>
                  <td className="column-name">
                    <strong>{columnCode || "—"}</strong>
                  </td>
                  <td className="column-name" lang="ja">
                    {names.ja || "—"}
                    {descriptionJa ? (
                      <small className="column-description">
                        {descriptionJa}
                      </small>
                    ) : null}
                  </td>
                  <td className="column-name" lang="en">
                    {names.en || "—"}
                    {descriptionEn ? (
                      <small className="column-description">
                        {descriptionEn}
                      </small>
                    ) : null}
                  </td>
                  <td>
                    <span className="data-type-chip">{p.data_type || "—"}</span>
                  </td>
                  <td>
                    <span
                      className={`nullable-status ${nullable === false ? "required" : "optional"}`}
                    >
                      <i aria-hidden="true" />
                      {nullable == null ? "—" : nullable ? "Yes" : "No"}
                    </span>
                  </td>
                  <td>{p.relation_table || "—"}</td>
                  <td className="column-note">{p.note || "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
