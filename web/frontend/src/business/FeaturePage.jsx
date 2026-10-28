import React, { useEffect, useState } from "react";
import { Check, Clock3, Download, Menu as MenuIcon, Monitor, Pencil, PlugZap, Upload } from "lucide-react";
import { businessApi } from "./api.js";
import { ProgressBar, RichText, Status } from "./ui.jsx";
import { reviewCounts } from "./text.js";

export default function FeaturePage({ itemId, tree, onTree, onOpen, onExport, onImport, notify }) {
  const [item, setItem] = useState(null);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ name: "", description: "" });
  const [parent, setParent] = useState("");
  const entry = findEntry(tree, itemId);

  useEffect(() => {
    setEditing(false);
    setError("");
    if (!itemId || entry) { setItem(null); return undefined; }
    let live = true;
    businessApi.item(itemId).then((body) => { if (live) setItem(body); }).catch((reason) => { if (live) setError(reason.message); });
    return () => { live = false; };
  }, [itemId, tree]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!itemId) return <Welcome tree={tree} onOpen={onOpen} />;
  if (entry) return <EntryPage entry={entry} tree={tree} parent={parent} setParent={setParent} onTree={onTree} onOpen={onOpen} notify={notify} />;
  if (error) return <div className="bm-page"><p className="bm-error">{error}</p></div>;
  if (!item) return <div className="bm-page"><p className="bm-muted">Đang tải…</p></div>;

  const counts = reviewCounts(item.parts || []);
  async function save(body, message) {
    try {
      const result = await businessApi.updateItem(item.id, body);
      onTree(result.tree);
      if (result.item) setItem(result.item);
      setEditing(false);
      notify(message);
    } catch (reason) {
      notify(reason.message);
    }
  }

  return <article className="bm-page">
    <header className="bm-page-head">
      <div className="bm-kind">{item.kind === "menu" ? <MenuIcon size={13} /> : item.batch ? <Clock3 size={13} /> : <Monitor size={13} />}
        {item.kind === "menu" ? "Menu" : item.batch ? "Tính năng · chạy nền" : "Tính năng"}</div>
      <h1>{item.name}</h1>
      {item.path?.length > 1 ? <div className="bm-muted">{item.path.slice(0, -1).map((step) => step.name).join(" / ")}</div> : null}
    </header>

    {editing ? <div className="bm-edit">
      <label className="bm-field">Tên<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
      <label className="bm-field">Mô tả<textarea rows={6} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} />
        <small>Đổi tên chỉ đổi phần hiển thị. Route và node gốc giữ nguyên.</small></label>
      <div className="bm-edit-foot"><button type="button" className="bm-btn" onClick={() => setEditing(false)}>Huỷ</button>
        <button type="button" className="bm-btn pri" onClick={() => save({ name: draft.name, description: draft.description }, "Đã lưu")}>Lưu</button></div>
    </div> : <>
      <div className="bm-row-tools">
        <Status status={item.status} by={item.reviewed_by} />
        <button type="button" className="bm-tool" onClick={() => { setDraft({ name: item.name, description: item.description || "" }); setEditing(true); }}><Pencil size={13} /> Sửa</button>
        {item.status !== "approved" && item.description ? <button type="button" className="bm-tool" onClick={() => save({ status: "approved" }, "Đã duyệt")}><Check size={13} /> Duyệt</button> : null}
      </div>
      {item.description ? <RichText text={item.description} /> : <p className="bm-muted">Chưa có mô tả. Bấm Sửa để viết, hoặc để AI viết khi xuất tính năng.</p>}
    </>}

    {item.kind !== "menu" || !item.unsorted ? <div className="bm-stats">
      <div><b>{item.flow_count}</b><span>luồng</span></div>
      <div><b>{counts.approved}/{counts.total}</b><span>phần đã duyệt</span></div>
      <div><b>{item.questions}</b><span>câu hỏi</span></div>
    </div> : null}

    {item.kind === "feature" ? <>
      <dl className="bm-entry">
        <div><dt>{item.batch ? "Jobnet" : "Route"}</dt><dd><code>{item.route}</code></dd></div>
        <div><dt>Điểm vào</dt><dd><code>{item.entry?.name}</code></dd></div>
        {item.apis?.length ? <div><dt>API</dt><dd>{item.apis.map((api) => <code key={api}>{api}</code>)}</dd></div> : null}
        {item.writes?.length ? <div><dt>Ghi bảng</dt><dd>{item.writes.map((name) => <code key={name}>{name}</code>)}</dd></div> : null}
        {item.reads?.length ? <div><dt>Đọc bảng</dt><dd>{item.reads.map((name) => <code key={name}>{name}</code>)}</dd></div> : null}
      </dl>
      <section className="bm-ai-card">
        <div><b>Viết tài liệu bằng AI</b><p>Xuất node, cạnh, đoạn code và template của tính năng này ({item.node_count} node) thành một file. Đưa file cho AI viết, rồi nhập lại kết quả tại đây.</p></div>
        <div className="bm-actions">
          <button type="button" className="bm-btn pri" onClick={() => onExport(item.id)}><Download size={14} /> Xuất cho AI</button>
          <button type="button" className="bm-btn" onClick={() => onImport(item.id)}><Upload size={14} /> Nhập kết quả</button>
        </div>
        {item.last_export ? <span className="bm-muted small">Lần xuất gần nhất: {item.last_export.export_id} · {item.last_export.created_at}</span> : null}
      </section>
      <section className="bm-list">
        <span className="bm-label">Luồng nghiệp vụ</span>
        {(item.children || []).map((flow) => <button key={flow.id} type="button" className="bm-card" onClick={() => onOpen(flow)}>
          <span className="bm-card-h"><b>{flow.name}</b>{flow.has_doc ? <ProgressBar parts={flow.parts} /> : <span className="bm-nodoc">chưa viết</span>}</span>
          {flow.summary ? <p>{flow.summary}</p> : null}
          <span className="bm-card-meta">{flow.has_doc ? `${reviewCounts(flow.parts).approved}/${flow.parts.length} phần đã duyệt` : "Chưa có tài liệu · các bước đã được tách từ graph"}
            {flow.questions ? <span className="bm-q">{flow.questions} câu hỏi</span> : null}</span>
        </button>)}
      </section>
    </> : null}

    {item.kind === "menu" ? <section className="bm-list">
      <span className="bm-label">{item.unsorted ? "Entry point" : "Tính năng"}</span>
      {(item.children || []).map((child) => <button key={child.id} type="button" className="bm-card" onClick={() => onOpen(child)}>
        <span className="bm-card-h"><b className={child.kind === "entry" ? "mono" : ""}>{child.name}</b>{child.parts?.length ? <ProgressBar parts={child.parts} /> : null}</span>
        <span className="bm-card-meta">{child.route ? <code>{child.route}</code> : null}{child.kind === "feature" ? <span>{child.flow_count} luồng</span> : <span>{child.node_type}</span>}
          {child.questions ? <span className="bm-q">{child.questions} câu hỏi</span> : null}</span>
      </button>)}
    </section> : null}
  </article>;
}

function findEntry(tree, id) {
  const unsorted = (tree?.menus || []).find((menu) => menu.unsorted);
  return unsorted?.children?.find((child) => child.id === id) || null;
}

function EntryPage({ entry, tree, parent, setParent, onTree, onOpen, notify }) {
  const features = (tree?.menus || []).flatMap((menu) => (menu.children || []).filter((child) => child.kind === "feature").map((child) => ({ ...child, menu: menu.name })));
  const target = parent || features[0]?.id || "";
  async function assign() {
    try {
      const result = await businessApi.updateItem(entry.id, { parent_id: target });
      onTree(result.tree);
      notify("Đã gán vào tính năng");
      const feature = features.find((child) => child.id === target);
      if (feature) onOpen(feature);
    } catch (reason) {
      notify(reason.message);
    }
  }
  return <article className="bm-page">
    <header className="bm-page-head">
      <div className="bm-kind"><PlugZap size={13} /> Entry point chưa gán</div>
      <h1 className="mono">{entry.name}</h1>
      <div className="bm-muted">{entry.node_type} · {entry.node_id}</div>
    </header>
    <p className="bm-prose">Extractor tìm thấy entry point này nhưng không tính năng nào gọi tới nó, nên nó chưa có tài liệu. Gán nó vào một tính năng để AI viết luồng của nó cùng tính năng đó.</p>
    <label className="bm-field">Gán vào tính năng
      <select value={target} onChange={(event) => setParent(event.target.value)}>
        {features.map((feature) => <option key={feature.id} value={feature.id}>{feature.menu} / {feature.name}</option>)}
      </select>
    </label>
    <div className="bm-actions"><button type="button" className="bm-btn pri" disabled={!target} onClick={assign}>Gán</button></div>
  </article>;
}

function Welcome({ tree, onOpen }) {
  const menus = (tree?.menus || []).filter((menu) => !menu.unsorted);
  return <article className="bm-page">
    <header className="bm-page-head">
      <h1>Hiểu hệ thống theo từng tính năng</h1>
      <p className="bm-lead">Mỗi menu là một nhóm tính năng, mỗi tính năng có các luồng nghiệp vụ. Tài liệu do AI viết từ graph, có trích dẫn tới từng node; bạn đọc, sửa và duyệt. Chọn một mục ở menu trái hoặc tìm ở ô phía trên.</p>
    </header>
    <section className="bm-list">
      <span className="bm-label">Menu</span>
      {menus.map((menu) => <button key={menu.id} type="button" className="bm-card" onClick={() => onOpen(menu)}>
        <span className="bm-card-h"><b>{menu.name}</b><ProgressBar parts={menu.parts || []} /></span>
        <span className="bm-card-meta"><span>{(menu.children || []).length} tính năng</span><span>{menu.flow_count} luồng</span>{menu.questions ? <span className="bm-q">{menu.questions} câu hỏi</span> : null}</span>
      </button>)}
      {!menus.length ? <p className="bm-muted">Graph chưa có màn hình (SCREEN) hay jobnet nào. Hãy chạy extract với source Angular hoặc jobnet.csv.</p> : null}
    </section>
  </article>;
}
