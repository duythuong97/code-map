import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Background, Controls, MarkerType, ReactFlow } from "@xyflow/react";
import { Check, Cpu, Download, Pencil, Upload, User, X } from "lucide-react";
import { businessApi } from "./api.js";
import { buildAutoLayout } from "../code-flow/layout.js";
import { api } from "../code-flow/api.js";
import { edgeColor, flowFiltersKey, flowKey } from "../code-flow/constants.js";
import { FlowNode } from "../code-flow/FlowNode.jsx";
import {
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

  return <div className="bm-flow">
    <article className="bm-doc" ref={docRef}>
      <nav className="bm-toc" aria-label="Mục lục">
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
        <span className="bm-seg"><button type="button" aria-pressed={mode === "graph"} onClick={() => setMode("graph")}>Graph</button><button type="button" aria-pressed={mode === "lane"} onClick={() => setMode("lane")}>Swimlane</button></span>
        <span className="bm-muted small">{nodes.length} node · {(data.edges || []).length} cạnh{activeStep ? ` · đang xem bước ${steps.findIndex((step) => step.id === activeStep) + 1}` : ""}{picking ? " · nhấn node để thêm hoặc bỏ khỏi bước" : ""}</span>
      </div>
      {mode === "graph"
        ? <FlowGraph nodes={nodes} edges={data.edges || []} steps={steps} stepNodes={stepNodes} activeStep={editingStep?.id || activeStep} selected={selected}
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
function StepFlowNode(props) {
  const tags = props.data.tags || [];
  return <>
    {tags.length ? <span className="bm-gtags">{tags.map((tag) => <b key={tag}>B{tag}</b>)}</span> : null}
    <FlowNode {...props} />
  </>;
}
const nodeTypes = { flowNode: StepFlowNode };
const SEMANTIC_LEVELS = [["SYSTEM", "System"], ["APPLICATION", "Application"], ["TECHNICAL", "Technical"]];
const MAX_EXPANSION_NODES = 200;

function toGraphNode(node) {
  return { ...node, technical_name: node.name || node.technical_name, default_display_name: node.display_name || node.name };
}

function FlowGraph({ nodes, edges, steps, stepNodes, activeStep, selected, picking, entry, onNodeClick, notify }) {
  const instance = useRef(null);
  const requests = useRef(new Map());
  const [semantic, setSemantic] = useState("APPLICATION");
  const [groups, setGroups] = useState({});
  const [groupStates, setGroupStates] = useState({});
  const [intents, setIntents] = useState({});
  const [localized, setLocalized] = useState({});
  const filters = flowFiltersKey(MAX_EXPANSION_NODES);

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
      setGroups((current) => mergeGroup(current, key, { nodes: data.nodes || [], edges: data.edges || [] }));
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
  }, [semantic, filters, loadGroup]);

  function changeSemantic(level) {
    if (level === semantic) return;
    requests.current.forEach((controller) => controller.abort());
    setSemantic(level);
    setGroups({});
    setGroupStates({});
    Object.values(intents).forEach((intent) => void loadGroup(intent.node, intent.mode, intent.direction, level));
  }

  function clearExpansions() {
    requests.current.forEach((controller) => controller.abort());
    setGroups({}); setGroupStates({}); setIntents({});
  }

  const activeIndex = steps.findIndex((step) => step.id === activeStep);
  const focus = activeIndex >= 0 ? new Set(stepNodes[activeIndex]) : null;
  const graph = useMemo(() => {
    const expanded = materializeGroups(null, groups);
    const byId = new Map(nodes.map((node) => [node.node_id, toGraphNode(node)]));
    expanded.nodes.forEach((node) => { if (!byId.has(node.node_id)) byId.set(node.node_id, { ...node, expanded: true }); });
    const allEdges = new Map(edges.map((edge) => [edge.edge_id, { ...edge, source_node_id: edge.source, target_node_id: edge.target }]));
    expanded.edges.forEach((edge) => {
      if (!allEdges.has(edge.edge_id) && byId.has(edge.source_node_id) && byId.has(edge.target_node_id)) allEdges.set(edge.edge_id, edge);
    });
    return { nodes: [...byId.values()], edges: [...allEdges.values()] };
  }, [nodes, edges, groups]);
  const layout = useMemo(() => buildAutoLayout(graph.nodes, graph.edges, entry, localized), [graph, entry, localized]);

  const flowNodes = layout.map((node) => {
    const tags = stepNodes.map((ids, index) => (ids.includes(node.node_id) ? index + 1 : 0)).filter(Boolean);
    const inFocus = focus?.has(node.node_id);
    return {
      id: node.node_id, type: "flowNode", position: node.position,
      className: [selected === node.node_id ? "bm-sel" : "", focus && !inFocus ? "bm-dim" : "", inFocus ? "bm-focus" : "", picking ? "bm-pick" : "", node.expanded ? "bm-expanded" : ""].filter(Boolean).join(" "),
      data: { node, tags, localized, groups, loadStates: groupStates, intents, onToggleFlow: toggleFlow, semantic, filters },
    };
  });
  const flowEdges = graph.edges.map((edge) => {
    const dim = focus && !(focus.has(edge.source_node_id) && focus.has(edge.target_node_id));
    return {
      id: edge.edge_id, source: edge.source_node_id, target: edge.target_node_id, label: edge.edge_type, type: "smoothstep",
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
  useEffect(() => { const timer = setTimeout(fit, 30); return () => clearTimeout(timer); }, [fit]);
  useEffect(() => {
    if (selected) instance.current?.fitView({ padding: 0.6, duration: 300, nodes: [{ id: selected }], maxZoom: 1 });
  }, [selected]);
  const expansions = Object.keys(intents).length;
  if (!nodes.length) return <div className="bm-canvas bm-canvas-empty"><p className="bm-muted">Luồng này chưa có node nào trong graph.</p></div>;
  return <div className="bm-canvas">
    <div className="bm-canvas-tools">
      <span className="bm-seg">{SEMANTIC_LEVELS.map(([level, label]) => <button key={level} type="button" aria-pressed={semantic === level} onClick={() => changeSemantic(level)}>{label}</button>)}</span>
      {expansions ? <button type="button" className="bm-btn sm" onClick={clearExpansions}>Bỏ {expansions} mở rộng</button> : null}
    </div>
    <ReactFlow nodes={flowNodes} edges={flowEdges} nodeTypes={nodeTypes} onInit={(value) => { instance.current = value; fit(); }}
      onNodeClick={(_, node) => onNodeClick(node.id)} nodesConnectable={false} minZoom={0.15} maxZoom={1.6} proOptions={{ hideAttribution: true }}>
      <Background gap={20} size={1.2} />
      <Controls showInteractive={false} />
    </ReactFlow>
  </div>;
}

function Swimlane({ steps, stepNodes, nodesById, activeStep, selected, onStep, onNode }) {
  const lanes = swimlanes(steps.map((step, index) => ({ ...step, nodes: stepNodes[index] })), nodesById);
  const columns = `150px repeat(${steps.length}, minmax(220px, 1fr))`;
  return <div className="bm-lanes-wrap">
    <div className="bm-lanes" style={{ gridTemplateColumns: columns }}>
      <div className="bm-lh" />
      {steps.map((step, index) => <button key={step.id} type="button" className={`bm-ch${activeStep === step.id ? " active" : ""}`} onClick={() => onStep(step.id)}>
        <small>BƯỚC {index + 1}</small><b>{step.name || "Chưa đặt tên"}</b></button>)}
      <div className="bm-lh">Ai thực hiện<small>tài liệu</small></div>
      {steps.map((step) => <div key={step.id} className={`bm-cell${activeStep === step.id ? " active" : ""}`}>{step.actor ? <div className="bm-lc txt">{step.actor}</div> : null}</div>)}
      <div className="bm-lh">Quy tắc<small>tài liệu</small></div>
      {steps.map((step) => <div key={step.id} className={`bm-cell${activeStep === step.id ? " active" : ""}`}>{(step.rules || []).map((rule, index) => <div key={index} className={`bm-lc txt rule-${rule.status}`}>◆ {rule.title}</div>)}</div>)}
      {lanes.map((lane) => <React.Fragment key={lane.key}>
        <div className="bm-lh">{lane.label}<small>{lane.types.map(typeLabel).join(", ").toLowerCase()}</small></div>
        {lane.cells.map((cell, index) => <div key={index} className={`bm-cell${activeStep === steps[index].id ? " active" : ""}`}>
          {cell.map((node) => <button key={node.node_id} type="button" className={`bm-lc${selected === node.node_id ? " sel" : ""}`} onClick={() => onNode(node.node_id)}>
            <span>{typeLabel(node.node_type)}</span>{node.name}</button>)}
        </div>)}
      </React.Fragment>)}
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
