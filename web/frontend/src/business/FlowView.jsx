import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Background, Controls, MarkerType, ReactFlow } from "@xyflow/react";
import { Check, Cpu, Download, PanelLeftClose, PanelLeftOpen, Pencil, Upload, User, X } from "lucide-react";
import { businessApi } from "./api.js";
import { buildAutoLayout } from "../code-flow/layout.js";
import { api } from "../code-flow/api.js";
import { edgeColor, flowFiltersKey, flowKey } from "../code-flow/constants.js";
import { FlowNode } from "../code-flow/FlowNode.jsx";
import { defaultIntentKeys, placeNodes, visibleBaseEdges } from "./graph-place.js";
import {
  expansionIntentKey,
  materializeGroups,
  mergeExpansionIntent,
  mergeGroup,
  removeExpansionIntent,
  removeGroup,
  removeGroupLoadState,
  setGroupLoadState,
} from "../flow-state.js";
import { Inline, RichText, RuleStatus, Status } from "./ui.jsx";
import { citedNames, flowParts, nodeLookup, reviewCounts, swimlanes, typeLabel } from "./text.js";


export default function FlowView({ flowId, onTree, onOpenItem, onExport, onImport, notify }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [activeStep, setActiveStep] = useState(null);
  const [selected, setSelected] = useState(null);
  const [mode, setMode] = useState("graph");
  const [editing, setEditing] = useState(null);
  const [draft, setDraft] = useState(null);
  const [picking, setPicking] = useState(false);
  const [docOpen, setDocOpen] = useState(() => { try { return localStorage.getItem("bm.doc.open") !== "0"; } catch { return true; } });
  const toggleDoc = (open) => { setDocOpen(open); try { localStorage.setItem("bm.doc.open", open ? "1" : "0"); } catch { /* ignore */ } };
  const docRef = useRef(null);

  useEffect(() => {
    let live = true;
    setData(null); setError(""); setActiveStep(null); setSelected(null); setEditing(null); setPicking(false);
    businessApi.flow(flowId).then((body) => { if (live) setData(body); }).catch((reason) => { if (live) setError(reason.message); });
    return () => { live = false; };
  }, [flowId]);

  const nodes = data?.nodes || [];
  const nodesById = useMemo(() => new Map(nodes.map((node) => [node.node_id, node])), [nodes]);
  const lookup = useMemo(() => nodeLookup(nodes), [nodes]);

  if (error) return <div className="bm-page"><p className="bm-error">{error}</p></div>;
  if (!data) return <div className="bm-page"><p className="bm-muted">Đang tải luồng…</p></div>;

  const flow = data.flow;
  const steps = flow.steps || [];
  const parts = flowParts(flow);
  const counts = reviewCounts(parts.map((part) => part.status));
  const openQuestions = (flow.questions || []).filter((question) => !question.answer).length;
  const editingStep = typeof editing === "string" && editing.startsWith("step:") ? steps.find((step) => `step:${step.id}` === editing) : null;
  const stepNodes = steps.map((step) => (editingStep && step.id === editingStep.id ? draft.nodes : step.nodes) || []);

  async function patch(part, body, message) {
    try {
      const result = await businessApi.updatePart(flowId, part, body);
      setData(result);
      onTree(result.tree);
      setEditing(null); setPicking(false);
      notify(message);
    } catch (reason) {
      notify(reason.message);
    }
  }

  function startEdit(part) {
    setPicking(false);
    if (part === "summary") setDraft({ name: flow.name || "", text: flow.summary?.text || "" });
    else if (part === "edge_cases") setDraft({ text: (flow.edge_cases?.items || []).join("\n\n") });
    else {
      const step = steps.find((candidate) => `step:${candidate.id}` === part);
      setDraft({ name: step.name || "", actor: step.actor || "", text: step.text || "", nodes: [...(step.nodes || [])], rules: (step.rules || []).map((rule) => ({ ...rule })) });
      setActiveStep(step.id);
    }
    setEditing(part);
  }

  function saveEdit() {
    if (editing === "summary") return patch("summary", { name: draft.name, text: draft.text }, "Đã lưu. Phần này sẽ không bị AI ghi đè.");
    if (editing === "edge_cases") return patch("edge_cases", { items: draft.text.split(/\n\s*\n/).map((value) => value.trim()).filter(Boolean) }, "Đã lưu");
    return patch(editing, { name: draft.name, actor: draft.actor, text: draft.text, nodes: draft.nodes, rules: draft.rules }, "Đã lưu. Phần này sẽ không bị AI ghi đè.");
  }

  function onNodeClick(nodeId) {
    if (picking && editingStep) {
      setDraft((current) => ({ ...current, nodes: current.nodes.includes(nodeId) ? current.nodes.filter((id) => id !== nodeId) : [...current.nodes, nodeId] }));
      return;
    }
    setSelected((current) => (current === nodeId ? null : nodeId));
    const ref = docRef.current?.querySelector(`.bm-step .bm-prose .bm-ref[data-node="${CSS.escape(nodeId)}"]`)
      || docRef.current?.querySelector(`.bm-ref[data-node="${CSS.escape(nodeId)}"]`);
    ref?.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function cite(nodeId) {
    setSelected(nodeId);
    setMode("graph");
  }

  async function answer(question, value) {
    try {
      const result = await businessApi.answer(flowId, question.id, value);
      setData(result);
      onTree(result.tree);
      notify("Đã lưu câu trả lời");
    } catch (reason) {
      notify(reason.message);
    }
  }

  const scrollTo = (id) => docRef.current?.querySelector(`#${id}`)?.scrollIntoView({ block: "start", behavior: "smooth" });
  const tools = (part, status) => editing === part ? null : <span className="bm-step-tools">
    <button type="button" className="bm-tool" onClick={(event) => { event.stopPropagation(); startEdit(part); }}><Pencil size={13} /> Sửa</button>
    {status !== "approved" ? <button type="button" className="bm-tool" onClick={(event) => { event.stopPropagation(); patch(part, { status: "approved" }, "Đã duyệt"); }}><Check size={13} /> Duyệt</button> : null}
  </span>;

  return <div className={`bm-flow${docOpen ? "" : " doc-closed"}`}>
    <article className="bm-doc" ref={docRef} hidden={!docOpen}>
      <nav className="bm-toc" aria-label="Mục lục">
        <button type="button" className="bm-toc-close" title="Thu gọn tài liệu" aria-label="Thu gọn tài liệu" onClick={() => toggleDoc(false)}><PanelLeftClose size={15} /></button>
        <button type="button" onClick={() => scrollTo("bm-sec-summary")}>Tổng quan</button>
        {steps.map((step, index) => <button key={step.id} type="button" aria-current={activeStep === step.id}
          onClick={() => { setActiveStep(step.id); scrollTo(`bm-step-${step.id}`); }}>{index + 1}. {step.name || "Chưa đặt tên"}</button>)}
        <button type="button" onClick={() => scrollTo("bm-sec-edge")}>Ngoại lệ</button>
        <button type="button" onClick={() => scrollTo("bm-sec-questions")}>Câu hỏi <i>{openQuestions}</i></button>
      </nav>
      <div className="bm-doc-in">
        <header className="bm-doc-head" id="bm-sec-summary">
          <div className="bm-crumbs">{(data.path || []).slice(0, -1).map((step) => <React.Fragment key={step.id}>
            <button type="button" onClick={() => onOpenItem(step)}>{step.name}</button><span>/</span></React.Fragment>)}<span>{flow.name}</span></div>
          <h1>{flow.name || "Luồng chưa đặt tên"}</h1>
          <div className="bm-doc-meta">
            {data.has_doc ? <span className="bm-review"><span className="bm-review-bar"><i style={{ width: `${(counts.approved / Math.max(counts.total, 1)) * 100}%` }} className="approved" /><i style={{ width: `${(counts.edited / Math.max(counts.total, 1)) * 100}%` }} className="edited" /></span>
              {counts.approved}/{counts.total} phần đã duyệt</span> : null}
            {flow.imported_at ? <span>AI viết · nhập lúc {flow.imported_at}</span> : data.has_doc ? <span>Viết tay</span> : <span>Chưa có tài liệu · các bước đã được tách từ graph. Xuất tính năng cho AI để viết.</span>}
          </div>
          <div className="bm-actions">
            <button type="button" className="bm-btn" onClick={() => onExport(data.feature_id)}><Download size={14} /> Xuất cho AI</button>
            <button type="button" className="bm-btn" onClick={() => onImport(data.feature_id)}><Upload size={14} /> Nhập kết quả AI</button>
          </div>
        </header>

        <section className="bm-block">
          <div className="bm-block-h"><h2>Tổng quan</h2>{flow.summary?.text ? <Status status={flow.summary.status} by={flow.summary.reviewed_by} /> : null}{tools("summary", flow.summary?.status)}</div>
          {editing === "summary" ? <div className="bm-edit">
            <label className="bm-field">Tên luồng<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
            <label className="bm-field">Tóm tắt<textarea rows={7} value={draft.text} onChange={(event) => setDraft({ ...draft, text: event.target.value })} /><small>Cách đoạn bằng một dòng trống. Gõ [[tên node]] để trích dẫn, **chữ** để nhấn mạnh.</small></label>
            <EditFoot onCancel={() => setEditing(null)} onSave={saveEdit} />
          </div> : flow.summary?.text ? <RichText text={flow.summary.text} lookup={lookup} selected={selected} onCite={cite} />
            : <p className="bm-muted">Chưa có tóm tắt. Xuất tính năng cho AI để viết, hoặc bấm Sửa để tự viết.</p>}
          {(flow.facts || []).length ? <dl className="bm-facts">{flow.facts.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl> : null}
        </section>

        <section className="bm-block">
          <div className="bm-block-h"><h2>Các bước</h2><span className="bm-muted small">{steps.length} bước · tách từ graph</span></div>
          <div className="bm-timeline">
            {steps.map((step, index) => {
              const part = `step:${step.id}`;
              return <div key={step.id} id={`bm-step-${step.id}`} className={`bm-step${activeStep === step.id ? " active" : ""}`}>
                <span className="bm-num">{index + 1}</span>
                <div className="bm-step-body" onClick={(event) => { if (!event.target.closest("button, input, textarea, details, a")) setActiveStep(activeStep === step.id ? null : step.id); }}>
                  <div className="bm-step-h"><h3>{step.name || <span className="bm-muted">Chưa đặt tên</span>}</h3>{tools(part, step.status)}</div>
                  <div className="bm-step-sub">
                    {step.actor ? <span className="bm-actor">{/hệ thống|system/i.test(step.actor) ? <Cpu size={13} /> : <User size={13} />}{step.actor}</span> : null}
                    {step.text ? <Status status={step.status} by={step.reviewed_by} /> : <span className="bm-muted">Chưa viết</span>}
                  </div>
                  {editing === part ? <StepEditor draft={draft} setDraft={setDraft} picking={picking} setPicking={setPicking} nodesById={nodesById}
                    onCancel={() => { setEditing(null); setPicking(false); }} onSave={saveEdit} /> : <>
                    <RichText text={step.text} lookup={lookup} selected={selected} onCite={cite} />
                    {(step.rules || []).length ? <div className="bm-rules">{step.rules.map((rule, position) => <div key={position} className="bm-rule">
                      <b>{rule.title}</b>
                      {rule.text ? <p><Inline text={rule.text} lookup={lookup} selected={selected} onCite={cite} /></p> : null}
                      <div className="bm-rule-foot">{rule.at ? <><span>Kiểm tra tại</span><Inline text={`[[${rule.at}]]`} lookup={lookup} selected={selected} onCite={cite} /></> : null}
                        {rule.evidence ? <code className="bm-evidence">{rule.evidence}</code> : null}<RuleStatus status={rule.status} /></div>
                    </div>)}</div> : null}
                    {(step.orphans || []).length ? <p className="bm-error small">Tham chiếu không có trong graph: {step.orphans.join(", ")}</p> : null}
                    <details className="bm-nodes"><summary>{(step.nodes || []).length} node liên quan</summary>
                      <div className="bm-node-list">{(step.nodes || []).map((id) => nodesById.get(id)).filter(Boolean).map((node) =>
                        <button key={node.node_id} type="button" className={`bm-ref${selected === node.node_id ? " sel" : ""}`} data-node={node.node_id} title={`${node.node_type} · ${node.node_id}`} onClick={() => cite(node.node_id)}>{node.name}</button>)}</div>
                    </details>
                  </>}
                </div>
              </div>;
            })}
          </div>
        </section>

        <section className="bm-block" id="bm-sec-edge">
          <div className="bm-block-h"><h2>Ngoại lệ và trường hợp đặc biệt</h2>{flow.edge_cases?.items?.length ? <Status status={flow.edge_cases.status} by={flow.edge_cases.reviewed_by} /> : null}{tools("edge_cases", flow.edge_cases?.status)}</div>
          {editing === "edge_cases" ? <div className="bm-edit">
            <label className="bm-field">Mỗi trường hợp một đoạn<textarea rows={6} value={draft.text} onChange={(event) => setDraft({ ...draft, text: event.target.value })} /></label>
            <EditFoot onCancel={() => setEditing(null)} onSave={saveEdit} />
          </div> : (flow.edge_cases?.items || []).length ? <ul className="bm-bullets">{flow.edge_cases.items.map((item, index) => <li key={index}><Inline text={item} lookup={lookup} selected={selected} onCite={cite} /></li>)}</ul>
            : <p className="bm-muted">Chưa có.</p>}
        </section>

        <section className="bm-block" id="bm-sec-questions">
          <div className="bm-block-h"><h2>Câu hỏi AI chưa chắc</h2><span className="bm-muted small">{openQuestions} đang chờ</span></div>
          <p className="bm-muted small">AI ghi lại những chỗ code không đủ để kết luận. Câu trả lời được lưu cùng tài liệu và gửi kèm ở lần xuất sau.</p>
          {(flow.questions || []).map((question) => <Question key={question.id} question={question} lookup={lookup} selected={selected} onCite={cite} onAnswer={answer} />)}
          {!(flow.questions || []).length ? <p className="bm-muted">Chưa có câu hỏi.</p> : null}
        </section>
      </div>
    </article>

    <section className="bm-stage">
      <div className="bm-stage-bar">
        {!docOpen ? <button type="button" className="bm-btn sm" onClick={() => toggleDoc(true)}><PanelLeftOpen size={14} /> Tài liệu</button> : null}
        <span className="bm-seg"><button type="button" aria-pressed={mode === "graph"} onClick={() => setMode("graph")}>Graph</button><button type="button" aria-pressed={mode === "lane"} onClick={() => setMode("lane")}>Swimlane</button></span>
        <span className="bm-muted small">{nodes.length} node · {(data.edges || []).length} cạnh{activeStep ? ` · đang xem bước ${steps.findIndex((step) => step.id === activeStep) + 1}` : ""}{picking ? " · nhấn node để thêm hoặc bỏ khỏi bước" : ""}</span>
      </div>
      {mode === "graph"
        ? <FlowGraph nodes={nodes} edges={data.edges || []} levels={data.levels} steps={steps} stepNodes={stepNodes} activeStep={editingStep?.id || activeStep} selected={selected}
            picking={picking} entry={steps[0]?.nodes?.[0]} onNodeClick={onNodeClick} notify={notify} />
        : <Swimlane steps={steps} stepNodes={stepNodes} nodesById={nodesById} activeStep={activeStep} selected={selected} onStep={setActiveStep} onNode={onNodeClick} />}
      {selected && mode === "graph" && !picking ? <NodeCard node={nodesById.get(selected)} flow={flow} onClose={() => setSelected(null)} onStep={(id) => { setActiveStep(id); docRef.current?.querySelector(`#bm-step-${id}`)?.scrollIntoView({ block: "start", behavior: "smooth" }); }} /> : null}
    </section>
  </div>;
}

function EditFoot({ onCancel, onSave }) {
  return <div className="bm-edit-foot"><span className="bm-muted small">Phần đã sửa tay được khoá khi xuất cho AI.</span>
    <span className="bm-actions"><button type="button" className="bm-btn" onClick={onCancel}>Huỷ</button><button type="button" className="bm-btn pri" onClick={onSave}>Lưu thay đổi</button></span></div>;
}

function StepEditor({ draft, setDraft, picking, setPicking, nodesById, onCancel, onSave }) {
  const setRule = (index, key, value) => setDraft({ ...draft, rules: draft.rules.map((rule, position) => (position === index ? { ...rule, [key]: value } : rule)) });
  return <div className="bm-edit">
    <label className="bm-field">Tên bước<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
    <label className="bm-field">Người / hệ thống thực hiện<input value={draft.actor} onChange={(event) => setDraft({ ...draft, actor: event.target.value })} /></label>
    <label className="bm-field">Nội dung<textarea rows={8} value={draft.text} onChange={(event) => setDraft({ ...draft, text: event.target.value })} />
      <small>Cách đoạn bằng một dòng trống. Gõ [[tên node]] để trích dẫn, **chữ** để nhấn mạnh.</small></label>
    {draft.rules.map((rule, index) => <div key={index} className="bm-field">
      <span>Quy tắc {index + 1}{rule.at ? ` · tại ${rule.at}` : ""}</span>
      <input value={rule.title} onChange={(event) => setRule(index, "title", event.target.value)} aria-label={`Tên quy tắc ${index + 1}`} />
      <textarea rows={3} value={rule.text} onChange={(event) => setRule(index, "text", event.target.value)} aria-label={`Nội dung quy tắc ${index + 1}`} />
    </div>)}
    <div className="bm-field"><span>Node của bước ({draft.nodes.length})</span>
      <div className="bm-node-list">{draft.nodes.map((id) => nodesById.get(id)).filter(Boolean).map((node) => <span key={node.node_id} className="bm-ref">{node.name}</span>)}</div>
      <button type="button" className={`bm-btn sm${picking ? " on" : ""}`} onClick={() => setPicking(!picking)}>{picking ? "Đang chọn trên graph…" : "Chọn lại node trên graph"}</button>
    </div>
    <EditFoot onCancel={onCancel} onSave={onSave} />
  </div>;
}

function Question({ question, lookup, selected, onCite, onAnswer }) {
  const [value, setValue] = useState("");
  return <div className={`bm-question${question.answer ? " done" : ""}`}>
    <p><Inline text={question.text} lookup={lookup} selected={selected} onCite={onCite} /></p>
    {question.answer ? <div className="bm-answer">{question.answer}</div>
      : <form onSubmit={(event) => { event.preventDefault(); if (value.trim()) onAnswer(question, value.trim()); }}>
        <input value={value} onChange={(event) => setValue(event.target.value)} placeholder="Trả lời…" aria-label="Trả lời" />
        <button type="submit" className="bm-btn sm">Lưu</button>
      </form>}
  </div>;
}

// Graph nodes are the explorer's FlowNode, so the 1/W/R/E checkboxes on both sides keep working:
// ticking one loads that expansion from /flow and adds its nodes around the flow.
const StepFlowNode = React.memo(function StepFlowNode(props) {
  const tags = props.data.tags || [];
  return <>
    {tags.length ? <span className="bm-gtags">{tags.map((tag) => <b key={tag}>B{tag}</b>)}</span> : null}
    <FlowNode {...props} />
  </>;
});
const nodeTypes = { flowNode: StepFlowNode };
const SEMANTIC_LEVELS = [["SYSTEM", "System"], ["APPLICATION", "Application"], ["TECHNICAL", "Technical"]];
const MAX_EXPANSION_NODES = 200;

// Expansions skip structural containment (table → columns, file → methods): one hop would otherwise pull in everything.
function withoutStructure(rootId, data) {
  const edges = (data.edges || []).filter((edge) => edge.graph_layer !== "STRUCTURAL" && edge.edge_type !== "HAS_COLUMN");
  const linked = new Set([rootId]);
  edges.forEach((edge) => { linked.add(edge.source_node_id); linked.add(edge.target_node_id); });
  return { nodes: (data.nodes || []).filter((node) => linked.has(node.node_id) && node.node_type !== "COLUMN"), edges };
}

function toGraphNode(node) {
  return { ...node, technical_name: node.name || node.technical_name, default_display_name: node.display_name || node.name };
}

function FlowGraph({ nodes: flowNodesAll, edges: flowEdgesAll, levels, steps, stepNodes, activeStep, selected, picking, entry, onNodeClick, notify }) {
  const instance = useRef(null);
  const canvasRef = useRef(null);
  const requests = useRef(new Map());
  const [semantic, setSemantic] = useState("TECHNICAL");
  // System/Application project the flow itself too, not only the expansions.
  const level = semantic !== "TECHNICAL" ? levels?.[semantic] : null;
  const nodes = useMemo(() => {
    if (!level) return flowNodesAll;
    const keep = new Set(level.nodes);
    return flowNodesAll.filter((node) => keep.has(node.node_id));
  }, [flowNodesAll, level]);
  const edges = level ? level.edges : flowEdgesAll;
  const [groups, setGroups] = useState({});
  const [groupStates, setGroupStates] = useState({});
  const [intents, setIntents] = useState({});
  const [localized, setLocalized] = useState({});
  const [suppressed, setSuppressed] = useState(() => new Set());
  // Nodes the user dragged keep their spot; `moving` holds the position while a drag is in progress.
  const [dragged, setDragged] = useState({});
  const [moving, setMoving] = useState(null);
  const filters = flowFiltersKey(MAX_EXPANSION_NODES);
  const baseEdges = useMemo(() => edges.map((edge) => ({ ...edge, source_node_id: edge.source, target_node_id: edge.target })), [edges]);
  const defaults = useMemo(() => defaultIntentKeys(baseEdges), [baseEdges]);

  useEffect(() => {
    const names = {};
    nodes.forEach((node) => {
      if (node.names?.ja || node.names?.en) names[node.node_id] = { ja: { name: node.names.ja }, en: { name: node.names.en } };
    });
    setLocalized((current) => ({ ...names, ...current }));
  }, [nodes]);

  useEffect(() => () => requests.current.forEach((controller) => controller.abort()), []);

  const loadGroup = useCallback(async (node, mode, direction, level) => {
    const key = flowKey(node.node_id, mode, direction, level, filters);
    requests.current.get(key)?.abort();
    const controller = new AbortController();
    requests.current.set(key, controller);
    setGroupStates((current) => setGroupLoadState(current, key, { loading: true, error: "", truncated: false }));
    try {
      const data = await api(`/flow?node_id=${encodeURIComponent(node.node_id)}&mode=${mode}&direction=${direction}&max_nodes=${MAX_EXPANSION_NODES}&semantic_level=${level}`, { signal: controller.signal });
      if (requests.current.get(key) !== controller) return;
      setGroups((current) => mergeGroup(current, key, withoutStructure(node.node_id, data)));
      setGroupStates((current) => setGroupLoadState(current, key, { loading: false, error: "", truncated: Boolean(data.truncated) }));
      const ids = (data.nodes || []).map((item) => item.node_id).slice(0, 300);
      if (ids.length) {
        const body = await api(`/localization?${ids.map((id) => `target_id=${encodeURIComponent(id)}`).join("&")}`).catch(() => null);
        if (body?.items) setLocalized((current) => ({ ...current, ...body.items }));
      }
      if (data.truncated) notify?.(`Đã cắt ở ${MAX_EXPANSION_NODES} node`);
    } catch (error) {
      if (error.name === "AbortError") return;
      setIntents((current) => removeExpansionIntent(current, node.node_id, mode, direction));
      setGroupStates((current) => setGroupLoadState(current, key, { loading: false, error: error.message, truncated: false }));
      notify?.(error.message);
    } finally {
      if (requests.current.get(key) === controller) requests.current.delete(key);
    }
  }, [filters, notify]);

  const toggleFlow = useCallback((node, mode, direction, checked) => {
    const intent = expansionIntentKey(node.node_id, mode, direction);
    if (defaults.has(intent)) {
      // A 1-hop the flow already shows: unticking hides those edges, ticking shows them again.
      setSuppressed((current) => {
        const next = new Set(current);
        if (checked) next.delete(intent); else next.add(intent);
        return next;
      });
      return;
    }
    const key = flowKey(node.node_id, mode, direction, semantic, filters);
    if (!checked) {
      requests.current.get(key)?.abort();
      setIntents((current) => removeExpansionIntent(current, node.node_id, mode, direction));
      setGroupStates((current) => removeGroupLoadState(current, key));
      setGroups((current) => removeGroup(current, key));
      return;
    }
    setIntents((current) => mergeExpansionIntent(current, node, mode, direction));
    void loadGroup(node, mode, direction, semantic);
  }, [semantic, filters, loadGroup, defaults]);

  function changeSemantic(level) {
    if (level === semantic) return;
    requests.current.forEach((controller) => controller.abort());
    setSemantic(level);
    setGroups({});
    setGroupStates({});
    setSuppressed(new Set());
    setDragged({});
    Object.values(intents).forEach((intent) => void loadGroup(intent.node, intent.mode, intent.direction, level));
  }

  function clearExpansions() {
    requests.current.forEach((controller) => controller.abort());
    setGroups({}); setGroupStates({}); setIntents({}); setSuppressed(new Set()); setDragged({});
  }

  const activeIndex = steps.findIndex((step) => step.id === activeStep);
  const focus = activeIndex >= 0 ? new Set(stepNodes[activeIndex]) : null;
  const graph = useMemo(() => {
    const expanded = materializeGroups(null, groups);
    const byId = new Map(nodes.map((node) => [node.node_id, toGraphNode(node)]));
    expanded.nodes.forEach((node) => { if (!byId.has(node.node_id)) byId.set(node.node_id, { ...node, expanded: true }); });
    const shown = new Map(visibleBaseEdges(baseEdges, suppressed).map((edge) => [edge.edge_id, edge]));
    expanded.edges.forEach((edge) => {
      if (!shown.has(edge.edge_id) && byId.has(edge.source_node_id) && byId.has(edge.target_node_id)) shown.set(edge.edge_id, edge);
    });
    // The flow's own nodes always stay; an expanded node stays while an edge still reaches it.
    const linked = new Set(nodes.map((node) => node.node_id));
    shown.forEach((edge) => { linked.add(edge.source_node_id); linked.add(edge.target_node_id); });
    const keep = [...byId.values()].filter((node) => linked.has(node.node_id));
    return { nodes: keep, edges: [...shown.values()] };
  }, [nodes, baseEdges, groups, suppressed, entry]);
  // The flow's own nodes are laid out once; expansions never move them.
  const basePositions = useMemo(() => new Map(buildAutoLayout(nodes.map(toGraphNode), baseEdges, entry, {}).map((node) => [node.node_id, node.position])), [nodes, baseEdges, entry]);
  const positions = useMemo(() => {
    const allEdges = [...baseEdges, ...Object.values(groups).flatMap((group) => group.edges || [])];
    const order = Object.values(groups).flatMap((group) => (group.nodes || []).map((node) => node.node_id));
    return placeNodes(new Map([...basePositions, ...Object.entries(dragged)]), [...nodes.map((node) => node.node_id), ...order], allEdges);
  }, [basePositions, nodes, baseEdges, groups, dragged]);
  const layout = graph.nodes.map((node) => ({ ...node, position: (moving?.id === node.node_id && moving.position) || positions.get(node.node_id) || { x: 0, y: 0 } }));
  const onNodesChange = useCallback((changes) => {
    for (const change of changes) {
      if (change.type !== "position") continue;
      if (change.dragging && change.position) setMoving({ id: change.id, position: change.position });
      else if (change.dragging === false) {
        setMoving((current) => {
          if (current?.id === change.id) setDragged((saved) => ({ ...saved, [change.id]: change.position || current.position }));
          return null;
        });
      }
    }
  }, []);
  const intentsShown = useMemo(() => {
    const merged = { ...intents };
    defaults.forEach((key) => { if (!suppressed.has(key)) merged[key] = true; });
    return merged;
  }, [intents, defaults, suppressed]);

  const flowNodes = layout.map((node) => {
    const tags = stepNodes.map((ids, index) => (ids.includes(node.node_id) ? index + 1 : 0)).filter(Boolean);
    const inFocus = focus?.has(node.node_id);
    return {
      id: node.node_id, type: "flowNode", position: node.position,
      className: [selected === node.node_id ? "bm-sel" : "", focus && !inFocus ? "bm-dim" : "", inFocus ? "bm-focus" : "", picking ? "bm-pick" : "", node.expanded ? "bm-expanded" : ""].filter(Boolean).join(" "),
      data: { node, tags, localized, groups, loadStates: groupStates, intents: intentsShown, onToggleFlow: toggleFlow, semantic, filters },
    };
  });
  const flowEdges = graph.edges.map((edge) => {
    const dim = focus && !(focus.has(edge.source_node_id) && focus.has(edge.target_node_id));
    return {
      id: edge.edge_id, source: edge.source_node_id, target: edge.target_node_id, label: edge.edge_type, type: "default",
      className: dim ? "bm-edge-dim" : "", animated: false,
      style: { stroke: edgeColor(edge.edge_type), strokeWidth: 1.6 },
      markerEnd: { type: MarkerType.ArrowClosed, color: edgeColor(edge.edge_type), width: 14, height: 14 },
      labelBgPadding: [6, 3], labelBgBorderRadius: 8,
    };
  });
  const fit = useCallback(() => {
    const ids = focus ? [...focus] : null;
    instance.current?.fitView({ padding: 0.15, duration: 300, minZoom: 0.65, maxZoom: 1, nodes: ids?.map((id) => ({ id })) });
  }, [activeIndex]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const timer = setTimeout(fit, 60);
    return () => clearTimeout(timer);
  }, [semantic]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { const timer = setTimeout(fit, 30); return () => clearTimeout(timer); }, [fit]);
  // Selecting a node never zooms; the view only pans when the node is off screen (e.g. picked from the doc).
  useEffect(() => {
    const flow = instance.current;
    const node = selected && flow?.getNode(selected);
    const box = canvasRef.current?.getBoundingClientRect();
    if (!node || !box) return;
    const point = flow.flowToScreenPosition(node.position);
    if (point.x > box.left && point.x < box.right - 120 && point.y > box.top && point.y < box.bottom - 60) return;
    flow.setCenter(node.position.x + 130, node.position.y + 50, { zoom: flow.getZoom(), duration: 300 });
  }, [selected]);
  const expansions = Object.keys(intents).length + suppressed.size;
  if (!flowNodesAll.length) return <div className="bm-canvas bm-canvas-empty"><p className="bm-muted">Luồng này chưa có node nào trong graph.</p></div>;
  return <div className="bm-canvas" ref={canvasRef}>
    <div className="bm-canvas-tools">
      <span className="bm-seg">{SEMANTIC_LEVELS.map(([level, label]) => <button key={level} type="button" aria-pressed={semantic === level} onClick={() => changeSemantic(level)}>{label}</button>)}</span>
      {expansions ? <button type="button" className="bm-btn sm" onClick={clearExpansions}>Về graph ban đầu</button> : null}
    </div>
    <ReactFlow nodes={flowNodes} edges={flowEdges} nodeTypes={nodeTypes} onInit={(value) => { instance.current = value; fit(); }}
      onNodeClick={(_, node) => onNodeClick(node.id)} onNodesChange={onNodesChange} nodesDraggable nodesConnectable={false} onlyRenderVisibleElements minZoom={0.15} maxZoom={1.6} proOptions={{ hideAttribution: true }}>
      <Background gap={20} size={1.2} />
      <Controls showInteractive={false} />
    </ReactFlow>
  </div>;
}

// Lanes are systems; each step is a card in the lane of the system it runs in, and arrows follow the
// step order, so the picture shows the flow moving from system to system.
function Swimlane({ steps, stepNodes, nodesById, activeStep, selected, onStep, onNode }) {
  const placed = steps.map((step, index) => ({ ...step, nodes: stepNodes[index] }));
  const { lanes, placement } = swimlanes(placed, nodesById);
  const board = useRef(null);
  const [arrows, setArrows] = useState([]);

  useEffect(() => {
    const draw = () => {
      const root = board.current;
      if (!root) return;
      const base = root.getBoundingClientRect();
      const boxes = steps.map((step) => root.querySelector(`[data-step="${CSS.escape(step.id)}"]`)?.getBoundingClientRect());
      const next = [];
      for (let index = 0; index + 1 < boxes.length; index += 1) {
        const from = boxes[index], to = boxes[index + 1];
        if (!from || !to) continue;
        const x1 = from.right - base.left, y1 = from.top + from.height / 2 - base.top;
        const x2 = to.left - base.left, y2 = to.top + to.height / 2 - base.top;
        const dx = Math.max(24, (x2 - x1) / 2);
        next.push({ id: `${steps[index].id}-${steps[index + 1].id}`, d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`,
          cross: placement[index] !== placement[index + 1] });
      }
      setArrows(next);
    };
    draw();
    const observer = new ResizeObserver(draw);
    if (board.current) observer.observe(board.current);
    return () => observer.disconnect();
  }, [steps, stepNodes, placement.join("|")]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!steps.length) return <div className="bm-canvas bm-canvas-empty"><p className="bm-muted">Luồng này chưa có bước nào.</p></div>;
  return <div className="bm-lanes-wrap">
    <div className="bm-swim" ref={board} style={{ gridTemplateColumns: `160px repeat(${steps.length}, 260px)`, gridTemplateRows: `auto repeat(${lanes.length}, minmax(120px, auto))` }}>
      <div className="bm-swim-corner" />
      {steps.map((step, index) => <div key={step.id} className="bm-swim-col" style={{ gridColumn: index + 2, gridRow: 1 }}>Bước {index + 1}</div>)}
      {lanes.map((lane, row) => <React.Fragment key={lane.key}>
        <div className="bm-swim-lane" style={{ gridColumn: 1, gridRow: row + 2 }}><b>{lane.label}</b><small>{lane.types.map(typeLabel).join(", ").toLowerCase()}</small></div>
        <div className="bm-swim-track" style={{ gridColumn: `2 / span ${steps.length}`, gridRow: row + 2 }} />
      </React.Fragment>)}
      {placed.map((step, index) => {
        const row = lanes.findIndex((lane) => lane.key === placement[index]) + 2;
        const nodes = (step.nodes || []).map((id) => nodesById.get(id)).filter(Boolean);
        return <div key={step.id} data-step={step.id} role="button" tabIndex={0}
          className={`bm-swim-step${activeStep === step.id ? " active" : ""}`} style={{ gridColumn: index + 2, gridRow: row }}
          onClick={() => onStep(step.id)} onKeyDown={(event) => { if (event.key === "Enter") onStep(step.id); }}>
          <span className="bm-swim-no">B{index + 1}</span>
          <b>{step.name || "Chưa đặt tên"}</b>
          {step.actor ? <span className="bm-swim-actor">{step.actor}</span> : null}
          {(step.rules || []).map((rule, position) => <span key={position} className={`bm-swim-rule rule-${rule.status || "warn"}`}>◆ {rule.title}</span>)}
          <span className="bm-swim-nodes">{nodes.map((node) => <button key={node.node_id} type="button"
            className={`bm-ref${selected === node.node_id ? " sel" : ""}${node.system !== placement[index] ? " other" : ""}`}
            title={`${typeLabel(node.node_type)} · ${node.system || ""}`} onClick={(event) => { event.stopPropagation(); onNode(node.node_id); }}>{node.name}</button>)}</span>
        </div>;
      })}
      <svg className="bm-swim-arrows" aria-hidden="true">
        <defs><marker id="bm-swim-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" /></marker></defs>
        {arrows.map((arrow) => <path key={arrow.id} d={arrow.d} className={arrow.cross ? "cross" : ""} markerEnd="url(#bm-swim-head)" />)}
      </svg>
    </div>
  </div>;
}

function NodeCard({ node, flow, onClose, onStep }) {
  if (!node) return null;
  const mentions = [];
  (flow.steps || []).forEach((step, index) => {
    if (citedNames(step.text).some((name) => name === node.name || name === node.ref)) mentions.push([step.id, `B${index + 1} · ${step.name || "Chưa đặt tên"}`]);
    (step.rules || []).forEach((rule) => { if (rule.at === node.name || rule.at_node_id === node.node_id) mentions.push([step.id, `Quy tắc: ${rule.title}`]); });
  });
  const inSteps = (flow.steps || []).map((step, index) => ((step.nodes || []).includes(node.node_id) ? `B${index + 1}` : "")).filter(Boolean);
  return <div className="bm-pop">
    <div className="bm-pop-h"><div><div className="bm-kind">{typeLabel(node.node_type)}</div><h4>{node.name}</h4></div>
      <button type="button" className="bm-icon-btn" onClick={onClose} aria-label="Đóng"><X size={14} /></button></div>
    <dl className="bm-kv">
      <dt>node_id</dt><dd><code>{node.node_id}</code></dd>
      {node.file ? <><dt>Nguồn</dt><dd><code>{node.file}{node.line ? `:${node.line}` : ""}</code></dd></> : null}
      {node.term ? <><dt>Từ điển</dt><dd>{node.term}</dd></> : null}
      {node.names?.ja || node.names?.en ? <><dt>JA / EN</dt><dd>{[node.names.ja, node.names.en].filter(Boolean).join(" · ")}</dd></> : null}
      <dt>Thuộc bước</dt><dd>{inSteps.length ? inSteps.join(", ") : "chưa thuộc bước nào"}</dd>
    </dl>
    {mentions.length ? <div className="bm-mentions"><span className="bm-label">Được nhắc trong tài liệu</span>
      {mentions.map(([stepId, label], index) => <button key={index} type="button" onClick={() => onStep(stepId)}>{label}</button>)}</div> : null}
  </div>;
}
