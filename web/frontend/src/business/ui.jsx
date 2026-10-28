import React, { useEffect } from "react";
import { X } from "lucide-react";
import { PART_LABELS, RULE_LABELS, inlineTokens, paragraphs, resolveCitation, reviewCounts } from "./text.js";

export function Status({ status, by }) {
  if (!status) return null;
  return <span className={`bm-status ${status}`}>{PART_LABELS[status] || status}{status === "approved" && by ? ` · ${by}` : ""}</span>;
}

export function RuleStatus({ status }) {
  return <span className={`bm-status rule-${status || "warn"}`}>{RULE_LABELS[status] || RULE_LABELS.warn}</span>;
}

export function ProgressBar({ parts = [], max = 8 }) {
  const shown = parts.slice(0, max);
  const counts = reviewCounts(parts);
  return <span className="bm-mbar" title={`${counts.approved}/${counts.total} phần đã duyệt`}>
    {shown.map((part, index) => <i key={index} className={part} />)}
  </span>;
}

// Renders document text: paragraphs, **bold**, `code` and [[node]] citations as links.
export function RichText({ text, lookup, selected, onCite }) {
  const blocks = paragraphs(text);
  if (!blocks.length) return null;
  return <div className="bm-prose">
    {blocks.map((block, index) => <p key={index}><Inline text={block} lookup={lookup} selected={selected} onCite={onCite} /></p>)}
  </div>;
}

export function Inline({ text, lookup, selected, onCite }) {
  return inlineTokens(text).map((token, index) => {
    if (token.type === "bold") return <strong key={index}>{token.value}</strong>;
    if (token.type === "code") return <code key={index}>{token.value}</code>;
    if (token.type === "ref") {
      const node = lookup ? resolveCitation(lookup, token.value) : null;
      if (!node) return <span key={index} className="bm-ref missing" title="Không có node này trong graph của luồng">{token.value}</span>;
      return <button key={index} type="button" className={`bm-ref${selected === node.node_id ? " sel" : ""}`} data-node={node.node_id}
        title={`${node.node_type} · ${node.node_id}`} onClick={(event) => { event.stopPropagation(); onCite?.(node.node_id); }}>{token.value}</button>;
    }
    return <React.Fragment key={index}>{token.value}</React.Fragment>;
  });
}

export function Modal({ title, children, footer, onClose, wide }) {
  useEffect(() => {
    const onKey = (event) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return <div className="bm-modal" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div className={`bm-modal-card${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
      <div className="bm-modal-head"><h3>{title}</h3><button type="button" className="bm-icon-btn" onClick={onClose} aria-label="Đóng"><X size={16} /></button></div>
      <div className="bm-modal-body">{children}</div>
      {footer ? <div className="bm-modal-foot">{footer}</div> : null}
    </div>
  </div>;
}

export function downloadText(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: filename.endsWith(".md") ? "text/markdown;charset=utf-8" : "text/yaml;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function copyText(text, element) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    element?.focus();
    element?.select();
    return false;
  }
}
