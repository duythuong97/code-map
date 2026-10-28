import React, { useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, Clock3, FileText, ListTree, Menu as MenuIcon, Monitor, PlugZap } from "lucide-react";
import { ProgressBar } from "./ui.jsx";

const FILTERS = [
  ["all", "Tất cả"],
  ["unreviewed", "Chưa duyệt"],
  ["questions", "Có câu hỏi"],
  ["nodoc", "Chưa viết"],
];

function matches(item, filter) {
  if (item.kind !== "flow") return false;
  if (filter === "questions") return item.questions > 0;
  if (filter === "unreviewed") return item.parts.includes("ai");
  if (filter === "nodoc") return !item.has_doc;
  return true;
}

function keep(item, filter) {
  return filter === "all" || matches(item, filter) || (item.children || []).some((child) => keep(child, filter));
}

function allFlows(items) {
  return items.flatMap((item) => (item.kind === "flow" ? [item] : allFlows(item.children || [])));
}

export function ancestorsOf(menus, id) {
  const walk = (items, trail) => {
    for (const item of items) {
      if (item.id === id) return trail;
      const found = walk(item.children || [], [...trail, item.id]);
      if (found) return found;
    }
    return null;
  };
  return walk(menus || [], []) || [];
}

function Icon({ item }) {
  if (item.kind === "menu") return item.unsorted ? <PlugZap size={13} /> : <MenuIcon size={13} />;
  if (item.kind === "feature") return item.batch ? <Clock3 size={13} /> : <Monitor size={13} />;
  if (item.kind === "entry") return <PlugZap size={13} />;
  return <FileText size={13} />;
}

export default function NavTree({ tree, selectedId, onSelect }) {
  const menus = tree?.menus || [];
  const [filter, setFilter] = useState("all");
  const [open, setOpen] = useState(() => new Set());
  const listRef = useRef(null);

  // Menus and features start open; the unassigned list starts closed.
  useEffect(() => {
    setOpen((current) => {
      if (current.size) return current;
      const next = new Set();
      menus.forEach((menu) => {
        if (menu.unsorted) return;
        next.add(menu.id);
        (menu.children || []).forEach((child) => next.add(child.id));
      });
      return next;
    });
  }, [menus]);

  useEffect(() => {
    if (!selectedId) return;
    const trail = ancestorsOf(menus, selectedId);
    if (trail.some((id) => !open.has(id))) setOpen((current) => new Set([...current, ...trail]));
  }, [selectedId, menus]); // eslint-disable-line react-hooks/exhaustive-deps

  const rows = useMemo(() => {
    const result = [];
    const walk = (items, depth) => items.forEach((item) => {
      if (!keep(item, filter)) return;
      result.push({ item, depth });
      if ((item.children || []).length && (filter !== "all" || open.has(item.id))) walk(item.children, depth + 1);
    });
    walk(menus, 0);
    return result;
  }, [menus, filter, open]);

  const flows = useMemo(() => allFlows(menus), [menus]);
  const counts = {
    all: flows.length,
    unreviewed: flows.filter((flow) => matches(flow, "unreviewed")).length,
    questions: flows.filter((flow) => matches(flow, "questions")).length,
    nodoc: flows.filter((flow) => matches(flow, "nodoc")).length,
  };
  const features = menus.reduce((sum, menu) => sum + (menu.children || []).filter((child) => child.kind === "feature").length, 0);

  function toggle(id) {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function onKeyDown(event) {
    const index = rows.findIndex((row) => row.item.id === selectedId);
    const row = rows[index];
    const go = (next) => { if (rows[next]) { onSelect(rows[next].item); focusRow(rows[next].item.id); } };
    if (event.key === "ArrowDown") go(index + 1);
    else if (event.key === "ArrowUp") go(Math.max(index - 1, 0));
    else if (event.key === "ArrowRight" && row?.item.children?.length && !open.has(row.item.id)) toggle(row.item.id);
    else if (event.key === "ArrowLeft" && row?.item.children?.length && open.has(row.item.id)) toggle(row.item.id);
    else if (event.key === "Enter" && row) onSelect(row.item, { open: true });
    else return;
    event.preventDefault();
  }

  function focusRow(id) {
    requestAnimationFrame(() => listRef.current?.querySelector(`[data-id="${CSS.escape(id)}"]`)?.focus());
  }

  return <nav className="bm-nav" aria-label="Menu và tính năng">
    <div className="bm-nav-head">
      <div><h2>Menu và tính năng</h2><p>{menus.filter((menu) => !menu.unsorted).length} menu · {features} tính năng · {flows.length} luồng</p></div>
      <span className="bm-nav-tools">
        <button type="button" className="bm-tool" title="Mở hết" aria-label="Mở hết" onClick={() => setOpen(new Set(menus.flatMap((menu) => [menu.id, ...(menu.children || []).map((child) => child.id)])))}><ListTree size={14} /></button>
      </span>
    </div>
    <div className="bm-nav-filters" role="group" aria-label="Lọc cây">
      {FILTERS.map(([key, label]) => <button key={key} type="button" className="bm-chip" aria-pressed={filter === key} onClick={() => setFilter(key)}>{label} <b>{counts[key]}</b></button>)}
    </div>
    <ul className="bm-tree" role="tree" ref={listRef} onKeyDown={onKeyDown}>
      {rows.map(({ item, depth }) => {
        const hasChildren = Boolean(item.children?.length);
        const expanded = filter !== "all" || open.has(item.id);
        return <li key={item.id} data-id={item.id} role="treeitem" aria-level={depth + 1} aria-selected={item.id === selectedId}
          aria-expanded={hasChildren ? expanded : undefined} tabIndex={item.id === selectedId ? 0 : -1}
          className={`bm-row k-${item.kind}${item.id === selectedId ? " sel" : ""}`} style={{ "--d": depth }}
          onClick={() => onSelect(item)}>
          {hasChildren
            ? <button type="button" tabIndex={-1} className="bm-chev" aria-label={expanded ? "Thu gọn" : "Mở rộng"} onClick={(event) => { event.stopPropagation(); toggle(item.id); }}><ChevronRight size={12} /></button>
            : <span />}
          <span className="bm-tic"><Icon item={item} /></span>
          <span className="bm-tname"><b>{item.name}</b></span>
          <span className="bm-tmeta">
            {item.questions ? <span className="bm-q">{item.questions} ?</span> : null}
            {item.kind === "flow" && !item.has_doc ? <span className="bm-nodoc">chưa viết</span> : null}
            {item.kind === "entry" ? <span className="bm-nodoc">chưa gán</span> : null}
            {(item.kind === "flow" && item.has_doc) || (item.kind !== "flow" && item.kind !== "entry" && !item.unsorted && item.parts?.length) ? <ProgressBar parts={item.parts} max={6} /> : null}
          </span>
        </li>;
      })}
      {!rows.length ? <li className="bm-empty">Không có mục nào khớp bộ lọc.</li> : null}
    </ul>
  </nav>;
}
