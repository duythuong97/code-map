import React, { useCallback, useEffect, useState } from "react";
import { Activity, Inbox, Monitor, Moon, PanelLeft, Scale, Sun, UserRound } from "lucide-react";
import "./business.css";
import { businessApi, currentUser, setCurrentUser } from "./api.js";
import NavTree from "./NavTree.jsx";
import SearchBox from "./SearchBox.jsx";
import FeaturePage from "./FeaturePage.jsx";
import FlowView from "./FlowView.jsx";
import NodePage from "./NodePage.jsx";
import { ExportDialog, ImportDialog } from "./AiDialogs.jsx";
import { KbStatusPage, KnowledgePage, ReviewPage } from "./KnowledgePages.jsx";
import { parseRoute, routeHash } from "./text.js";


const THEMES = [["system", "Theo hệ điều hành", Monitor], ["light", "Sáng", Sun], ["dark", "Tối", Moon]];

function readTheme() {
  try { return localStorage.getItem("bm.theme") || "system"; } catch { return "system"; }
}

function applyTheme(theme) {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
}

applyTheme(readTheme());

function ThemeToggle() {
  const [theme, setTheme] = useState(readTheme);
  useEffect(() => {
    applyTheme(theme);
    try { localStorage.setItem("bm.theme", theme); } catch { /* ignore */ }
  }, [theme]);
  return <span className="bm-seg bm-theme" role="group" aria-label="Giao diện sáng hoặc tối">
    {THEMES.map(([key, label, Icon]) => <button key={key} type="button" title={label} aria-label={label} aria-pressed={theme === key} onClick={() => setTheme(key)}><Icon size={14} /></button>)}
  </span>;
}

// The name recorded on approvals and answers (sent as X-Code-Map-User).
function UserName() {
  const [name, setName] = useState(currentUser);
  return <label className="bm-user" title="Tên của bạn: ghi lại người duyệt, người trả lời">
    <UserRound size={14} aria-hidden="true" />
    <input value={name} placeholder="Tên của bạn" aria-label="Tên của bạn" maxLength={100}
      onChange={(event) => { setName(event.target.value); setCurrentUser(event.target.value.trim()); }} />
  </label>;
}

function findItem(tree, id) {
  const stack = [...(tree?.menus || [])];
  while (stack.length) {
    const item = stack.pop();
    if (item.id === id) return item;
    stack.push(...(item.children || []));
  }
  return null;
}

export default function BusinessApp() {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  const [tree, setTree] = useState(null);
  const [error, setError] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [dialog, setDialog] = useState(null);
  const [toast, setToast] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [waiting, setWaiting] = useState(null);

  useEffect(() => {
    const onHash = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    businessApi.tree().then(setTree).catch((reason) => setError(reason.message));
  }, []);

  // How much waits for a person; refreshed when the page changes (approving elsewhere lowers it).
  useEffect(() => {
    businessApi.reviewQueue().then((queue) => setWaiting(Object.values(queue.counts || {}).reduce((sum, value) => sum + value, 0)))
      .catch(() => setWaiting(null));
  }, [route.view, refresh]);

  useEffect(() => {
    if (!toast) return undefined;
    const timer = setTimeout(() => setToast(""), 2600);
    return () => clearTimeout(timer);
  }, [toast]);

  const go = useCallback((view, id) => {
    window.location.hash = routeHash(view, id);
    setDrawer(false);
  }, []);

  const openItem = useCallback((item) => {
    if (!item) return;
    go(item.kind === "flow" ? "flow" : "feature", item.id);
  }, [go]);

  function pickSearch(hit) {
    if (hit.group === "node") { go("node", hit.id); return; }
    if (hit.kind === "flow" || hit.kind === "step" || hit.kind === "rule") go("flow", hit.id);
    else go("feature", hit.id);
  }

  function toggleNav() {
    if (window.matchMedia("(max-width: 1080px)").matches) setDrawer(!drawer);
    else setCollapsed(!collapsed);
  }

  const onTree = useCallback((next) => { if (next) setTree(next); }, []);
  const featureOf = (id) => findItem(tree, id);
  const exportDialog = (featureId) => setDialog({ type: "export", featureId });
  const importDialog = (featureId) => setDialog({ type: "import", featureId });

  const selectedId = route.id;
  return <div className="bm">
    <header className="bm-top">
      <span className="bm-brand">
        <button type="button" className="bm-icon-btn" onClick={toggleNav} aria-label="Ẩn hoặc hiện menu" aria-expanded={!collapsed}><PanelLeft size={16} /></button>
        <a className="bm-logo" href="#/"><span className="bm-logo-mark" aria-hidden="true" />Code Map</a>
      </span>
      <SearchBox onPick={pickSearch} />
      <span className="bm-top-r">
        <a className={`bm-toplink${route.view === "review" ? " on" : ""}`} href="#/review"><Inbox size={14} /> Chờ duyệt{waiting ? <b>{waiting}</b> : null}</a>
        <a className={`bm-toplink${route.view === "knowledge" ? " on" : ""}`} href="#/knowledge"><Scale size={14} /> Quy tắc</a>
        <a className={`bm-toplink${route.view === "kb" ? " on" : ""}`} href="#/kb"><Activity size={14} /> KB</a>
        <UserName />
        <ThemeToggle />
      </span>
    </header>
    <div className={`bm-body${collapsed ? " collapsed" : ""}${drawer ? " drawer" : ""}`}>
      {tree ? <NavTree tree={tree} selectedId={selectedId} onSelect={openItem} /> : <nav className="bm-nav"><p className="bm-muted bm-pad">{error || "Đang tải cây tính năng…"}</p></nav>}
      {drawer ? <div className="bm-scrim" onClick={() => setDrawer(false)} /> : null}
      <main className="bm-main">
        {error ? <div className="bm-page"><p className="bm-error">{error}</p></div>
          : !tree ? null
            : route.view === "review"
              ? <div className="bm-page-wrap"><ReviewPage key={refresh} onOpenItem={openItem} onOpenNode={(id) => go("node", id)} notify={setToast} /></div>
            : route.view === "knowledge"
              ? <div className="bm-page-wrap"><KnowledgePage onOpenNode={(id) => go("node", id)} notify={setToast} /></div>
            : route.view === "kb"
              ? <div className="bm-page-wrap"><KbStatusPage onReview={() => go("review")} /></div>
            : route.view === "flow" && selectedId
              ? <FlowView key={`${selectedId}:${refresh}`} flowId={selectedId} onTree={onTree} onOpenItem={openItem} onOpenNode={(id) => go("node", id)} onExport={exportDialog} onImport={importDialog} notify={setToast} />
              : route.view === "node" && selectedId
                ? <NodePage key={selectedId} nodeId={selectedId} onOpenItem={openItem} onOpenNode={(id) => go("node", id)} notify={setToast} />
              : <div className="bm-page-wrap"><FeaturePage key={selectedId || "home"} itemId={selectedId} tree={tree} onTree={onTree} onOpen={openItem} onExport={exportDialog} onImport={importDialog} notify={setToast} /></div>}
      </main>
    </div>
    {dialog?.type === "export" ? <ExportDialog featureId={dialog.featureId} featureName={featureOf(dialog.featureId)?.name || ""} notify={setToast}
      onExported={() => businessApi.tree().then(setTree).catch(() => {})} onClose={() => setDialog(null)} /> : null}
    {dialog?.type === "import" ? <ImportDialog featureId={dialog.featureId} featureName={featureOf(dialog.featureId)?.name || ""} notify={setToast}
      onImported={(next) => { onTree(next); setRefresh((value) => value + 1); }} onClose={() => setDialog(null)} /> : null}
    {toast ? <div className="bm-toast" role="status">{toast}</div> : null}
  </div>;
}
