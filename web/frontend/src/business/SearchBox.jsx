import React, { useEffect, useRef, useState } from "react";
import { Search } from "lucide-react";
import { businessApi } from "./api.js";
import { typeLabel } from "./text.js";

const GROUPS = [
  ["feature", "Menu & tính năng"],
  ["flow", "Luồng nghiệp vụ"],
  ["step", "Bước"],
  ["rule", "Quy tắc"],
  ["node", "Node trong code"],
];

export default function SearchBox({ onPick }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [results, setResults] = useState([]);
  const [state, setState] = useState({ loading: false, error: "" });
  const [active, setActive] = useState(0);
  const input = useRef(null);
  const box = useRef(null);

  useEffect(() => {
    const term = query.trim();
    if (!term) { setResults([]); return undefined; }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setState({ loading: true, error: "" });
      try {
        const body = await businessApi.search(term, controller.signal);
        const business = (body.business || []).map((hit) => ({ ...hit, group: hit.kind === "menu" ? "feature" : hit.kind }));
        const nodes = (body.nodes || []).map((hit) => ({ ...hit, group: "node" }));
        setResults([...business, ...nodes]);
        setActive(0);
        setState({ loading: false, error: "" });
      } catch (error) {
        if (error.name !== "AbortError") setState({ loading: false, error: error.message });
      }
    }, 180);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [query]);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "/" && !event.target.closest("input, textarea, select")) { event.preventDefault(); input.current?.focus(); }
    };
    const onClick = (event) => { if (!box.current?.contains(event.target)) setOpen(false); };
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => { window.removeEventListener("keydown", onKey); document.removeEventListener("mousedown", onClick); };
  }, []);

  const ordered = GROUPS.flatMap(([group]) => results.filter((hit) => hit.group === group));

  function pick(hit) {
    if (!hit) return;
    setOpen(false);
    input.current?.blur();
    onPick(hit);
  }

  function onKeyDown(event) {
    if (event.key === "ArrowDown") { setActive((value) => Math.min(value + 1, ordered.length - 1)); event.preventDefault(); }
    else if (event.key === "ArrowUp") { setActive((value) => Math.max(value - 1, 0)); event.preventDefault(); }
    else if (event.key === "Enter") { pick(ordered[active]); event.preventDefault(); }
    else if (event.key === "Escape") { setOpen(false); input.current?.blur(); }
  }

  let position = -1;
  return <div className="bm-search" ref={box}>
    <label className="bm-search-box">
      <Search size={16} aria-hidden="true" />
      <input ref={input} type="search" value={query} autoComplete="off" aria-label="Tìm kiếm"
        placeholder="Tìm nghiệp vụ, bước, quy tắc, method, API, bảng…"
        onChange={(event) => { setQuery(event.target.value); setOpen(true); }} onFocus={() => setOpen(true)} onKeyDown={onKeyDown} />
      <kbd>/</kbd>
    </label>
    {open && query.trim() ? <div className="bm-drop" role="listbox">
      {state.error ? <p className="bm-drop-empty">{state.error}</p> : null}
      {!state.error && !ordered.length ? <p className="bm-drop-empty">{state.loading ? "Đang tìm…" : `Không có kết quả cho “${query}”.`}</p> : null}
      {GROUPS.map(([group, title]) => {
        const hits = results.filter((hit) => hit.group === group);
        if (!hits.length) return null;
        return <React.Fragment key={group}>
          <h6>{title}</h6>
          {hits.map((hit) => {
            position += 1;
            const index = position;
            const disabled = hit.group === "node" && !hit.feature_id;
            return <button key={`${hit.group}:${hit.id}:${hit.step_id || ""}:${index}`} type="button" role="option" aria-selected={index === active}
              className={`bm-hit${index === active ? " kb" : ""}`} disabled={disabled} onMouseEnter={() => setActive(index)} onClick={() => pick(hit)}>
              <span className="bm-hit-ic">{hit.group === "node" ? hit.node_type.slice(0, 2) : hit.group === "rule" ? "◆" : hit.group === "step" ? "B" : hit.group === "flow" ? "≡" : "▣"}</span>
              <span className="bm-hit-t"><b className={hit.group === "node" ? "mono" : ""}>{hit.label}</b>
                <small>{hit.group === "node" ? [typeLabel(hit.node_type), hit.path || "chưa thuộc tính năng nào"].join(" · ") : hit.path || hit.route || ""}</small></span>
            </button>;
          })}
        </React.Fragment>;
      })}
    </div> : null}
  </div>;
}
