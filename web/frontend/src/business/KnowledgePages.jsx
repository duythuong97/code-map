import React, { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Check, CircleHelp, FileText, Gavel, Pencil, Plus, RefreshCw, Scale, X } from "lucide-react";
import { businessApi, currentUser } from "./api.js";

// Review queue, rules / decisions / questions and the knowledge-base status page.
// Approving needs a name (top bar); the backend refuses an approval without one.

export const RULE_STATUS_LABELS = {
  draft: "Bản nháp · chưa duyệt",
  approved: "Đã duyệt",
  needs_review: "Code đã đổi · cần duyệt lại",
  retired: "Ngừng dùng",
};
const DECISION_LABELS = { proposed: "Chờ quyết định", accepted: "Đã chốt", rejected: "Không chọn", superseded: "Đã thay thế" };
const QUESTION_LABELS = { open: "Chờ trả lời", answered: "Đã trả lời", dropped: "Đã bỏ" };

function useLoad(load, deps) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const reload = useCallback(() => {
    setError("");
    return load().then(setData).catch((reason) => setError(reason.message));
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { reload(); }, [reload]);
  return { data, error, reload };
}

function needName(notify) {
  if (currentUser()) return false;
  notify("Nhập tên của bạn ở góc trên bên phải trước khi duyệt: lần duyệt ghi lại người duyệt.");
  return true;
}

function Pill({ kind, children }) {
  return <span className={`bm-status kn-${kind}`}>{children}</span>;
}

function Nodes({ ids = [], names = {}, onOpenNode }) {
  if (!ids.length) return null;
  return <span className="kn-nodes">{ids.map((nodeId) => <button key={nodeId} type="button" className="bm-ref" onClick={() => onOpenNode(nodeId)} title={nodeId}>
    {names[nodeId] || nodeId}
  </button>)}</span>;
}

function Section({ icon: Icon, title, count, hint, children }) {
  return <section className="kn-section">
    <h2><Icon size={15} /> {title} <span className="kn-count">{count}</span></h2>
    {hint ? <p className="bm-muted small">{hint}</p> : null}
    {count ? <div className="kn-list">{children}</div> : <p className="bm-muted small">Không có mục nào.</p>}
  </section>;
}

// --------------------------------------------------------------------------- rules

function RuleEditor({ rule, onSave, onCancel }) {
  const [draft, setDraft] = useState({
    title: rule?.title || "", text: rule?.text || "", evidence: rule?.evidence || "", domain: rule?.domain || "",
    nodes: (rule?.node_ids || []).join(", "), approve: false,
  });
  const field = (key) => ({ value: draft[key], onChange: (event) => setDraft({ ...draft, [key]: event.target.value }) });
  function submit() {
    const body = { title: draft.title, text: draft.text, evidence: draft.evidence,
      nodes: draft.nodes.split(",").map((value) => value.trim()).filter(Boolean) };
    if (!rule) body.domain = draft.domain;
    if (!rule && draft.approve) body.status = "approved";
    onSave(body);
  }
  return <div className="bm-edit kn-editor">
    <label className="bm-field">Quy tắc (một câu khẳng định)<input {...field("title")} placeholder="Đơn đã giao không được hủy" /></label>
    <label className="bm-field">Giải thích<textarea rows={3} {...field("text")} placeholder="Không đạt thì sao, ngoại lệ là gì" /></label>
    <div className="kn-grid">
      <label className="bm-field">Evidence (file:dòng)<input {...field("evidence")} placeholder="order-db/order_flow.sql:112" /></label>
      {!rule ? <label className="bm-field">Domain<input {...field("domain")} placeholder="ORD" /></label> : null}
    </div>
    <label className="bm-field">Node hiện thực quy tắc (tên kỹ thuật hoặc node id, cách nhau bằng dấu phẩy)
      <input {...field("nodes")} placeholder="PKG_ORDER.CANCEL_ORDER, ORDER_HEADER" />
      <small>Khi code của các node này đổi hành vi, quy tắc đã duyệt tự quay về hàng chờ duyệt.</small></label>
    {!rule ? <label className="kn-check"><input type="checkbox" checked={draft.approve} onChange={(event) => setDraft({ ...draft, approve: event.target.checked })} /> Duyệt luôn (tôi là người nắm nghiệp vụ này)</label> : null}
    <div className="bm-edit-foot"><button type="button" className="bm-btn" onClick={onCancel}>Huỷ</button>
      <button type="button" className="bm-btn pri" onClick={submit} disabled={!draft.title.trim()}>Lưu</button></div>
  </div>;
}

function RuleCard({ rule, onChanged, onOpenNode, notify }) {
  const [editing, setEditing] = useState(false);
  async function update(body, message) {
    if (body.status === "approved" && needName(notify)) return;
    try {
      await businessApi.updateRule(rule.rule_id, body);
      setEditing(false);
      notify(message);
      onChanged();
    } catch (reason) { notify(reason.message); }
  }
  if (editing) return <RuleEditor rule={rule} onSave={(body) => update(body, "Đã lưu; quy tắc cần được duyệt lại")} onCancel={() => setEditing(false)} />;
  return <div className={`kn-card rule-${rule.status}`}>
    <div className="kn-card-h"><code>{rule.rule_id}</code><b>{rule.title}</b><Pill kind={rule.status}>{RULE_STATUS_LABELS[rule.status] || rule.status}</Pill></div>
    {rule.status === "needs_review" && rule.review_reason ? <p className="kn-reason"><AlertTriangle size={13} /> {rule.review_reason}</p> : null}
    {rule.text ? <p>{rule.text}</p> : null}
    <div className="kn-meta">
      <span>Evidence: {rule.evidence ? <code>{rule.evidence}</code> : <em>chưa có</em>}</span>
      <span>Nguồn: {rule.source}{rule.proposed_by ? ` · đề xuất bởi ${rule.proposed_by}` : ""}{rule.approved_by ? ` · duyệt bởi ${rule.approved_by}` : ""}</span>
      <Nodes ids={rule.node_ids} names={rule.node_names} onOpenNode={onOpenNode} />
    </div>
    <div className="bm-actions">
      {rule.status !== "approved" ? <button type="button" className="bm-btn pri sm" onClick={() => update({ status: "approved" }, `Đã duyệt ${rule.rule_id}`)}><Check size={13} /> {rule.status === "needs_review" ? "Vẫn đúng · duyệt lại" : "Duyệt"}</button> : null}
      <button type="button" className="bm-btn sm" onClick={() => setEditing(true)}><Pencil size={13} /> Sửa</button>
      {rule.status !== "retired" ? <button type="button" className="bm-btn ghost sm" onClick={() => update({ status: "retired" }, `${rule.rule_id} ngừng dùng`)}><X size={13} /> Ngừng dùng</button> : null}
    </div>
  </div>;
}

// --------------------------------------------------------------------------- questions and decisions

function QuestionCard({ question, onChanged, onOpenNode, notify }) {
  const [answer, setAnswer] = useState("");
  async function send(body, message) {
    if (needName(notify)) return;
    try {
      await businessApi.answerQuestion(question.question_id, body);
      notify(message);
      onChanged();
    } catch (reason) { notify(reason.message); }
  }
  return <div className="kn-card">
    <div className="kn-card-h"><code>{question.question_id}</code><b>{question.text}</b><Pill kind={question.status}>{QUESTION_LABELS[question.status]}</Pill></div>
    {question.context ? <p className="bm-muted">{question.context}</p> : null}
    <div className="kn-meta"><span>Hỏi bởi {question.asked_by || "?"}</span><Nodes ids={question.node_ids} names={question.node_names} onOpenNode={onOpenNode} /></div>
    {question.status === "answered" ? <p className="kn-answer"><b>Trả lời ({question.answered_by}):</b> {question.answer}</p> : null}
    {question.status === "open" ? <>
      {question.options?.length ? <div className="kn-options">{question.options.map((option) => <button key={option.title} type="button"
        className={`bm-chip${answer === option.title ? " on" : ""}`} aria-pressed={answer === option.title} onClick={() => setAnswer(option.title)}
        title={option.consequence || ""}>{option.title}{option.consequence ? <small> · {option.consequence}</small> : null}</button>)}</div> : null}
      <label className="bm-field"><textarea rows={2} value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="Câu trả lời (AI sẽ coi là sự thật và không hỏi lại)" /></label>
      <div className="bm-actions">
        <button type="button" className="bm-btn pri sm" disabled={!answer.trim()} onClick={() => send({ answer }, `Đã trả lời ${question.question_id}`)}><Check size={13} /> Trả lời</button>
        <button type="button" className="bm-btn ghost sm" onClick={() => send({ status: "dropped" }, `Đã bỏ ${question.question_id}`)}>Bỏ câu hỏi</button>
      </div>
    </> : null}
  </div>;
}

function DecisionCard({ decision, onChanged, onOpenNode, notify }) {
  const [text, setText] = useState(decision.decision || "");
  const [reason, setReason] = useState(decision.reason || "");
  async function decide(status) {
    if (needName(notify)) return;
    try {
      await businessApi.updateDecision(decision.decision_id, { status, decision: text, reason });
      notify(status === "accepted" ? `Đã chốt ${decision.decision_id}` : `Đã từ chối ${decision.decision_id}`);
      onChanged();
    } catch (failure) { notify(failure.message); }
  }
  return <div className="kn-card">
    <div className="kn-card-h"><code>{decision.decision_id}</code><b>{decision.title}</b><Pill kind={decision.status}>{DECISION_LABELS[decision.status]}</Pill></div>
    {decision.context ? <p>{decision.context}</p> : null}
    {decision.options?.length ? <ol className="kn-option-list">{decision.options.map((option) => <li key={option.title}>
      <button type="button" className="bm-link" onClick={() => setText(option.title)} disabled={decision.status !== "proposed"}>{option.title}</button>
      {option.consequence ? <span className="bm-muted"> · {option.consequence}</span> : null}</li>)}</ol> : null}
    <div className="kn-meta"><Nodes ids={decision.node_ids} names={decision.node_names} onOpenNode={onOpenNode} />
      {decision.decided_by ? <span>Quyết định bởi {decision.decided_by}</span> : null}</div>
    {decision.status === "proposed" ? <>
      <label className="bm-field">Quyết định<textarea rows={2} value={text} onChange={(event) => setText(event.target.value)} /></label>
      <label className="bm-field">Lý do<textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
      <div className="bm-actions">
        <button type="button" className="bm-btn pri sm" disabled={!text.trim()} onClick={() => decide("accepted")}><Gavel size={13} /> Chốt</button>
        <button type="button" className="bm-btn ghost sm" onClick={() => decide("rejected")}>Từ chối</button>
      </div>
    </> : decision.decision ? <p className="kn-answer"><b>Quyết định:</b> {decision.decision}{decision.reason ? <><br /><b>Lý do:</b> {decision.reason}</> : null}</p> : null}
  </div>;
}

// --------------------------------------------------------------------------- pages

export function ReviewPage({ onOpenItem, onOpenNode, notify }) {
  const { data, error, reload } = useLoad(() => businessApi.reviewQueue(), []);
  if (error) return <div className="bm-page"><p className="bm-error">{error}</p></div>;
  if (!data) return <div className="bm-page"><p className="bm-muted">Đang tải…</p></div>;
  const props = { onChanged: reload, onOpenNode, notify };
  const flowLink = (part) => <button key={`${part.flow_id}:${part.part}`} type="button" className="bm-card" onClick={() => onOpenItem({ kind: "flow", id: part.flow_id })}>
    <span className="bm-card-h"><b>{part.flow_name || part.flow_id}</b><span className="bm-muted small">{part.part}</span></span>
    <span className="bm-card-meta"><span>{part.title}</span></span>
  </button>;
  return <article className="bm-page kn-page">
    <header className="bm-page-head">
      <div className="bm-kind"><Scale size={13} /> Kiến thức</div>
      <h1>Chờ duyệt</h1>
      <p className="bm-lead">Mọi thứ AI đề xuất và mọi kiến thức đã duyệt mà code đã đổi từ lúc duyệt. AI không tự duyệt được gì; câu trả lời và quyết định ở đây trở thành sự thật cho mọi agent.</p>
      <div className="bm-row-tools"><button type="button" className="bm-btn sm" onClick={reload}><RefreshCw size={13} /> Tải lại</button></div>
    </header>
    <Section icon={AlertTriangle} title="Quy tắc đã duyệt nhưng code đã đổi" count={data.rules_needs_review.length}
      hint="Hành vi của node hiện thực quy tắc đã khác lúc duyệt. Đọc lại code (evidence) rồi duyệt lại, sửa hoặc ngừng dùng.">
      {data.rules_needs_review.map((rule) => <RuleCard key={rule.rule_id} rule={rule} {...props} />)}
    </Section>
    <Section icon={CircleHelp} title="Câu hỏi chờ trả lời" count={data.questions_open.length}>
      {data.questions_open.map((question) => <QuestionCard key={question.question_id} question={question} {...props} />)}
    </Section>
    <Section icon={Gavel} title="Quyết định chờ chốt" count={data.decisions_proposed.length}>
      {data.decisions_proposed.map((decision) => <DecisionCard key={decision.decision_id} decision={decision} {...props} />)}
    </Section>
    <Section icon={Scale} title="Quy tắc AI đề xuất" count={data.rules_draft.length}>
      {data.rules_draft.map((rule) => <RuleCard key={rule.rule_id} rule={rule} {...props} />)}
    </Section>
    <Section icon={AlertTriangle} title="Tài liệu luồng đã duyệt nhưng code đã đổi" count={data.flow_parts_changed.length}>
      {data.flow_parts_changed.map(flowLink)}
    </Section>
    <Section icon={FileText} title="Tài liệu luồng AI viết, chưa duyệt" count={data.flow_parts_ai.length}>
      {data.flow_parts_ai.map(flowLink)}
    </Section>
  </article>;
}

const TABS = [["rules", "Quy tắc"], ["decisions", "Quyết định"], ["questions", "Câu hỏi"]];

export function KnowledgePage({ onOpenNode, notify }) {
  const [tab, setTab] = useState("rules");
  const [status, setStatus] = useState("");
  const [creating, setCreating] = useState(false);
  const load = () => (tab === "rules" ? businessApi.rules(status ? { status } : {})
    : tab === "decisions" ? businessApi.decisions(status ? { status } : {}) : businessApi.questions(status ? { status } : {}));
  const { data, error, reload } = useLoad(load, [tab, status]);
  const statuses = tab === "rules" ? RULE_STATUS_LABELS : tab === "decisions" ? DECISION_LABELS : QUESTION_LABELS;
  const props = { onChanged: reload, onOpenNode, notify };

  async function create(body) {
    if (body.status === "approved" && needName(notify)) return;
    try {
      const created = tab === "rules" ? await businessApi.createRule(body)
        : tab === "decisions" ? await businessApi.createDecision(body) : await businessApi.createQuestion(body);
      setCreating(false);
      notify(`Đã tạo ${created.rule_id || created.decision_id || created.question_id}`);
      reload();
    } catch (reason) { notify(reason.message); }
  }

  return <article className="bm-page kn-page">
    <header className="bm-page-head">
      <div className="bm-kind"><Scale size={13} /> Kiến thức</div>
      <h1>Quy tắc, quyết định và câu hỏi</h1>
      <p className="bm-lead">Kiến thức chỉ con người nói được, gắn với node trong graph. MCP server trả về cho AI kèm trạng thái duyệt.</p>
    </header>
    <div className="bm-row-tools">
      <span className="bm-seg" role="group" aria-label="Loại kiến thức">{TABS.map(([key, label]) => <button key={key} type="button" aria-pressed={tab === key}
        onClick={() => { setTab(key); setStatus(""); setCreating(false); }}>{label}</button>)}</span>
      <select className="bm-nav-select" value={status} onChange={(event) => setStatus(event.target.value)} aria-label="Lọc theo trạng thái">
        <option value="">Mọi trạng thái</option>
        {Object.entries(statuses).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select>
      <button type="button" className="bm-btn sm" onClick={() => setCreating(!creating)}><Plus size={13} /> Thêm</button>
    </div>
    {creating ? (tab === "rules" ? <RuleEditor onSave={create} onCancel={() => setCreating(false)} />
      : <SimpleCreate kind={tab} onSave={create} onCancel={() => setCreating(false)} />) : null}
    {error ? <p className="bm-error">{error}</p> : !data ? <p className="bm-muted">Đang tải…</p>
      : !data.length ? <p className="bm-muted">Chưa có mục nào.</p>
        : <div className="kn-list">{data.map((item) => tab === "rules" ? <RuleCard key={item.rule_id} rule={item} {...props} />
          : tab === "decisions" ? <DecisionCard key={item.decision_id} decision={item} {...props} />
            : <QuestionCard key={item.question_id} question={item} {...props} />)}</div>}
  </article>;
}

function SimpleCreate({ kind, onSave, onCancel }) {
  const [draft, setDraft] = useState({ title: "", context: "", options: "", nodes: "" });
  const field = (key) => ({ value: draft[key], onChange: (event) => setDraft({ ...draft, [key]: event.target.value }) });
  const list = (value) => value.split(/\n|,/).map((item) => item.trim()).filter(Boolean);
  function submit() {
    const common = { context: draft.context, nodes: list(draft.nodes), options: list(draft.options).map((title) => ({ title })) };
    onSave(kind === "decisions" ? { ...common, title: draft.title } : { ...common, text: draft.title });
  }
  return <div className="bm-edit kn-editor">
    <label className="bm-field">{kind === "decisions" ? "Cần quyết định gì" : "Câu hỏi"}<input {...field("title")} /></label>
    <label className="bm-field">Bối cảnh<textarea rows={3} {...field("context")} /></label>
    <label className="bm-field">Phương án (mỗi dòng một phương án)<textarea rows={3} {...field("options")} /></label>
    <label className="bm-field">Node liên quan (tên kỹ thuật, cách nhau bằng dấu phẩy)<input {...field("nodes")} /></label>
    <div className="bm-edit-foot"><button type="button" className="bm-btn" onClick={onCancel}>Huỷ</button>
      <button type="button" className="bm-btn pri" onClick={submit} disabled={!draft.title.trim()}>Lưu</button></div>
  </div>;
}

function freshness(stale) {
  const commits = [...new Set((stale.revisions || []).map((item) => String(item.commit || "").slice(0, 8)).filter(Boolean))];
  const built = `Graph extract lúc ${String(stale.generated_at || "?").replace("T", " ").slice(0, 16)}${commits.length ? ` từ commit ${commits.join(", ")}` : ""}`;
  if (stale.fresh === null || stale.fresh === undefined) return `${built} · chưa kiểm tra được độ mới (${stale.reason || "không thấy source"})`;
  if (stale.fresh) return `${built} · khớp với code hiện tại.`;
  const counts = stale.counts || {};
  return `${built} · ĐÃ CŨ: ${counts.changed || 0} file đã sửa, ${counts.deleted || 0} file đã xoá, ${counts.added || 0} file mới. Hãy chạy extract lại.`;
}

export function KbStatusPage({ onReview }) {
  const { data, error, reload } = useLoad(() => businessApi.kbStatus(), []);
  if (error) return <div className="bm-page"><p className="bm-error">{error}</p></div>;
  if (!data) return <div className="bm-page"><p className="bm-muted">Đang tải…</p></div>;
  const stale = data.stale || {};
  const counts = stale.counts || {};
  const review = data.review || {};
  const waiting = Object.values(review).reduce((sum, value) => sum + value, 0);
  return <article className="bm-page kn-page">
    <header className="bm-page-head">
      <div className="bm-kind"><RefreshCw size={13} /> Kiến thức</div>
      <h1>Tình trạng knowledge base</h1>
      <p className="bm-lead">{freshness(stale)}</p>
      <div className="bm-row-tools"><button type="button" className="bm-btn sm" onClick={reload}><RefreshCw size={13} /> Tải lại</button>
        <button type="button" className="bm-btn sm" onClick={onReview}>Chờ duyệt: {waiting}</button></div>
    </header>
    <div className="kn-stats">
      <div className={counts.changed ? "warn" : ""}><b>{counts.changed ?? "?"}</b><span>file đã sửa từ lúc extract</span></div>
      <div className={counts.added ? "warn" : ""}><b>{counts.added ?? "?"}</b><span>file mới chưa có trong graph</span></div>
      <div className={review.rules_needs_review ? "warn" : ""}><b>{review.rules_needs_review ?? 0}</b><span>quy tắc cần duyệt lại</span></div>
      <div className={review.questions_open ? "warn" : ""}><b>{review.questions_open ?? 0}</b><span>câu hỏi chờ trả lời</span></div>
    </div>
    <section className="kn-section"><h2>Source và commit</h2>
      <table className="kn-table"><thead><tr><th>Source</th><th>Commit lúc extract</th><th>Branch</th><th>Commit mới từ đó</th></tr></thead>
        <tbody>{(stale.revisions || []).map((item) => <tr key={item.source}><td>{item.source}</td>
          <td><code>{item.vcs === "none" ? "không có git" : String(item.commit || "").slice(0, 10)}</code>{item.dirty_at_extract ? " (có sửa chưa commit)" : ""}</td>
          <td>{item.branch || ""}</td><td>{item.commits_since ?? "?"}</td></tr>)}</tbody></table>
      {(stale.changed || []).length ? <details className="kn-details"><summary>File đã sửa ({stale.changed.length})</summary>
        <ul>{stale.changed.map((path) => <li key={path}><code>{path}</code></li>)}</ul></details> : null}
      {(stale.added || []).length ? <details className="kn-details"><summary>File mới ({stale.added.length})</summary>
        <ul>{stale.added.map((path) => <li key={path}><code>{path}</code></li>)}</ul></details> : null}
    </section>
    <section className="kn-section"><h2>Chất lượng extract</h2>
      <table className="kn-table"><tbody>{Object.entries(data.quality || {}).map(([key, value]) => <tr key={key}><td>{key}</td><td>{String(value)}</td></tr>)}</tbody></table>
    </section>
    <section className="kn-section"><h2>AI agent dùng KB thế nào (30 ngày)</h2>
      {(data.usage?.by_tool || []).length ? <table className="kn-table"><thead><tr><th>Tool</th><th>Lượt gọi</th><th>Không có kết quả</th><th>Người dùng</th></tr></thead>
        <tbody>{data.usage.by_tool.map((row) => <tr key={row.tool}><td><code>{row.tool}</code></td><td>{row.calls}</td><td>{row.empty}</td><td>{row.users}</td></tr>)}</tbody></table>
        : <p className="bm-muted small">Chưa có lượt gọi MCP nào.</p>}
      {(data.usage?.unanswered || []).length ? <>
        <p className="bm-muted small">Câu hỏi không có kết quả: chỗ KB đang thiếu (thuật ngữ chưa có trong từ điển, source chưa extract…).</p>
        <ul className="kn-unanswered">{data.usage.unanswered.map((row, index) => <li key={index}><code>{row.tool}</code> {row.arguments} <span className="bm-muted">×{row.count}</span></li>)}</ul>
      </> : null}
    </section>
  </article>;
}
