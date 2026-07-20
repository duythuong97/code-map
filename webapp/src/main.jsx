import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Background,
  BaseEdge,
  Handle,
  EdgeLabelRenderer,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  useReactFlow,
} from "@xyflow/react";
import {
  Box,
  Code2,
  Database,
  FileCode2,
  GitBranch,
  Hash,
  List,
  Lock,
  Maximize2,
  Minus,
  Plus,
  Search,
} from "lucide-react";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "./components/ui/table";

const flowColors = {
  READS: "#2563eb",
  WRITES: "#16a34a",
  DERIVES: "#9333ea",
  CALLS: "#f97316",
  TRIGGERS: "#dc2626",
  REMOTE_READS: "#0891b2",
  USES: "#14b8a6",
};

const DETAIL_WIDTH_KEY = "code-map.detailWidth";
const DEFAULT_DETAIL_WIDTH = 620;
const MIN_DETAIL_WIDTH = 360;
const MAX_DETAIL_WIDTH = 980;
const EDGE_TYPE_DEFAULTS = [
  "CALLS",
  "READS",
  "REMOTE_READS",
  "WRITES",
  "DERIVES",
  "TRIGGERS",
  "USES",
];
const READ_FLOW_TYPES = ["READS", "REMOTE_READS"];
const WRITE_FLOW_TYPES = ["WRITES"];
const EVERY_FLOW_TYPES = EDGE_TYPE_DEFAULTS;

const icons = {
  box: Box,
  code: Code2,
  database: Database,
  "file-code": FileCode2,
  "git-branch": GitBranch,
  hash: Hash,
  list: List,
};

const nodeTypes = {
  Table: { icon: Database, className: "table" },
  Procedure: { icon: Code2, className: "code" },
  SQLFunction: { icon: Box, className: "code" },
  Function: { icon: Box, className: "code" },
  Trigger: { icon: GitBranch, className: "trigger" },
  PLSQLPackage: { icon: FileCode2, className: "file" },
  Cursor: { icon: FileCode2, className: "file" },
  Sequence: { icon: Hash, className: "sequence" },
  SourceFile: { icon: FileCode2, className: "source-file" },
  Repository: { icon: Code2, className: "repository" },
  Application: { icon: Box, className: "application" },
};
const rfNodeTypes = { flowNode: FlowNode };
const edgeTypes = { lane: LaneEdge };
const basePath = window.CODE_MAP_CONFIG?.urlPrefix || "";
async function api(path, options = {}) {
  const response = await fetch(`${basePath}${path}`, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || `Request failed (${response.status})`);
  return payload;
}
const shortName = (qname = "") => qname.split(":").pop() || qname;
const objectName = (qname = "") =>
  shortName(qname).split(".").pop() || shortName(qname);
const schemaName = (qname = "") => shortName(qname).split(".")[0] || "";
function tableDisplayParts(label = "", qname = "") {
  const code = objectName(qname);
  const match = String(label).match(/^(.+?)\s*\((.*)\s+-\s+(.*)\)$/);
  if (!match) return { code, nameJa: "-", nameEn: "-" };
  return {
    code: match[1] || code,
    nameJa: match[2] || "-",
    nameEn: match[3] || "-",
  };
}
const rootView = (row) => ({
  name: row.display?.title || row.name || shortName(row.qualified_name),
  type: row.label,
  schema: row.display?.scope || row.display?.subtitle || "",
  subtitle: row.display?.subtitle,
  display: row.display,
  style: row.style,
  qname: row.qualified_name,
  nodeId: row.qualified_name,
});

function App() {
  const resizeStartRef = useRef(null);
  const searchAbortRef = useRef(null);
  const rootAbortRef = useRef(null);
  const detailAbortRef = useRef(null);
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchState, setSearchState] = useState({ status: "idle", error: "" });
  const [flowState, setFlowState] = useState({ status: "idle", error: "", truncated: false });
  const [detailState, setDetailState] = useState({ status: "idle", error: "" });
  const [schemas, setSchemas] = useState([]);
  const [schema, setSchema] = useState("");
  const [roots, setRoots] = useState([]);
  const [selected, setSelected] = useState(null);
  const [flow, setFlow] = useState({ nodes: [], edges: [] });
  const [focusNodeId, setFocusNodeId] = useState("");
  const [selection, setSelection] = useState(null);
  const [detail, setDetail] = useState(null);
  const [contract, setContract] = useState({ nodes: nodeTypes, edges: {} });
  const [detailOpen, setDetailOpen] = useState(true);
  const [detailWidth, setDetailWidth] = useState(() => {
    const stored = Number(localStorage.getItem(DETAIL_WIDTH_KEY));
    if (!Number.isFinite(stored)) return DEFAULT_DETAIL_WIDTH;
    return Math.min(MAX_DETAIL_WIDTH, Math.max(MIN_DETAIL_WIDTH, stored));
  });

  useEffect(() => {
    localStorage.setItem(DETAIL_WIDTH_KEY, String(detailWidth));
  }, [detailWidth]);

  useEffect(() => {
    const onPointerMove = (event) => {
      if (!resizeStartRef.current) return;
      const { startX, startWidth } = resizeStartRef.current;
      const nextWidth = startWidth + event.clientX - startX;
      setDetailWidth(
        Math.min(MAX_DETAIL_WIDTH, Math.max(MIN_DETAIL_WIDTH, nextWidth)),
      );
    };
    const onPointerUp = () => {
      resizeStartRef.current = null;
      document.body.classList.remove("resizing-detail");
    };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
    };
  }, []);

  const startDetailResize = (event) => {
    event.preventDefault();
    resizeStartRef.current = {
      startX: event.clientX,
      startWidth: detailWidth,
    };
    document.body.classList.add("resizing-detail");
  };
  const resizeDetailWithKeyboard = (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const delta = event.key === "ArrowRight" ? 24 : -24;
    setDetailWidth((width) => Math.min(MAX_DETAIL_WIDTH, Math.max(MIN_DETAIL_WIDTH, width + delta)));
  };

  useEffect(() => {
    api("/api/schemas").then((rows) =>
      setSchemas(Array.isArray(rows) ? rows : []),
    ).catch(() => setSchemas([]));
    api("/api/graph-contract").then((data) =>
      setContract({ nodes: data.nodes || nodeTypes, edges: data.edges || {} }),
    ).catch(() => {});
  }, []);

  useEffect(() => {
    const term = query.trim();
    if (!term) {
      searchAbortRef.current?.abort();
      setRoots([]);
      setSearchState({ status: "idle", error: "" });
      return undefined;
    }
    const controller = new AbortController();
    searchAbortRef.current?.abort();
    searchAbortRef.current = controller;
    const t = setTimeout(async () => {
      setSearchState({ status: "loading", error: "" });
      const schemaArg = schema ? `&schema=${encodeURIComponent(schema)}` : "";
      try {
        const rows = await api(`/api/search?q=${encodeURIComponent(term)}${schemaArg}`, { signal: controller.signal });
        const results = Array.isArray(rows) ? rows.map(rootView) : [];
        setRoots(results);
        setSearchState({ status: results.length ? "ready" : "empty", error: "" });
      } catch (error) {
        if (error.name !== "AbortError") setSearchState({ status: "error", error: error.message });
      }
    }, 180);
    return () => {
      clearTimeout(t);
      controller.abort();
    };
  }, [query, schema]);

  async function loadNodeDetail(qname) {
    const controller = new AbortController();
    detailAbortRef.current?.abort();
    detailAbortRef.current = controller;
    setDetail(null);
    setDetailState({ status: "loading", error: "" });
    try {
      const data = await api(`/api/node-detail?qname=${encodeURIComponent(qname)}`, { signal: controller.signal });
      setDetail(data);
      setDetailState({ status: "ready", error: "" });
    } catch (error) {
      if (error.name !== "AbortError") setDetailState({ status: "error", error: error.message });
    }
  }

  async function selectRoot(root) {
    if (!root) return;
    const controller = new AbortController();
    rootAbortRef.current?.abort();
    rootAbortRef.current = controller;
    setQuery(root.name || objectName(root.qname));
    setSearchOpen(false);
    setSelected(root);
    setFocusNodeId(root.nodeId);
    setFlowState({ status: "loading", error: "", truncated: false });
    const initialGraph = toGraph(
      { label: root.type, qualified_name: root.qname, name: root.name, display: root.display, style: root.style },
      [],
      contract,
    );
    setFlow(initialGraph);
    setSelection({ type: "node", item: initialGraph.nodes[0] });
    setDetailOpen(true);
    loadNodeDetail(root.qname);
    try {
      const data = await api(`/api/flow?qname=${encodeURIComponent(root.qname)}`, { signal: controller.signal });
      const graph = toGraph(
        data.node || { label: root.type, qualified_name: root.qname, name: root.name, display: root.display },
        data.flows || [],
        data.contract || contract,
      );
      const layouted = applyStableLayout(graph, root.qname);
      setFlow(layouted);
      setSelection({ type: "node", item: layouted.nodes.find((node) => node.id === root.qname) || layouted.nodes[0] });
      setFlowState({ status: "ready", error: "", truncated: Boolean(data.truncated) });
    } catch (error) {
      if (error.name !== "AbortError") setFlowState({ status: "error", error: error.message, truncated: false });
    }
  }

  async function submitSearch() {
    const rawTerm = query.trim();
    const term = rawTerm.toLowerCase();
    if (!term) return;
    const controller = new AbortController();
    searchAbortRef.current?.abort();
    searchAbortRef.current = controller;
    setSearchState({ status: "loading", error: "" });
    const schemaArg = schema ? `&schema=${encodeURIComponent(schema)}` : "";
    try {
      const rows = await api(
        `/api/search?q=${encodeURIComponent(rawTerm)}${schemaArg}`,
        { signal: controller.signal },
      );
      const matches = Array.isArray(rows) ? rows.map(rootView) : [];
      setRoots(matches);
      if (!matches.length) {
        setSearchOpen(true);
        setSearchState({ status: "empty", error: "" });
        return;
      }
      setSearchState({ status: "ready", error: "" });
      const exact = matches.find((root) => {
        const values = [root.name, objectName(root.qname), root.qname]
          .filter(Boolean)
          .map((value) => value.toLowerCase());
        return values.includes(term);
      });
      await selectRoot(exact || matches[0]);
    } catch (error) {
      if (error.name !== "AbortError") {
        setSearchOpen(true);
        setSearchState({ status: "error", error: error.message });
      }
    }
  }

  return (
    <div
      className="app-shell dark"
      style={{ "--detail-width": `${detailWidth}px` }}
    >
      <aside className="sidebar">
        <div className="search-row">
          <select
            aria-label="Schema"
            value={schema}
            onChange={(e) => setSchema(e.target.value)}
          >
            <option value="">All</option>
            {schemas.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <div className="search-field">
            <label className="search-box" aria-label="Search nodes">
              <Search size={17} />
              <input
                role="combobox"
                aria-expanded={searchOpen && roots.length > 0}
                aria-controls="node-search-options"
                aria-autocomplete="list"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setSearchOpen(true);
                }}
                onFocus={() => setSearchOpen(true)}
                onKeyDown={(e) => {
                  if (e.key !== "Enter") return;
                  e.preventDefault();
                  submitSearch();
                }}
                placeholder="Search file / repository / application / database object..."
              />
            </label>
            {searchOpen && query.trim() && searchState.status === "loading" ? (
              <p className="empty-search" role="status">Searching…</p>
            ) : null}
            {searchOpen && query.trim() && searchState.status === "empty" ? (
              <p className="empty-search" role="status">No results. Try another name or schema.</p>
            ) : null}
            {searchOpen && query.trim() && searchState.status === "error" ? (
              <p className="state-banner error" role="alert">{searchState.error}</p>
            ) : null}
            {searchOpen && query.trim() && roots.length ? (
              <nav
                id="node-search-options"
                className="root-list"
                aria-label="Search results"
                role="listbox"
              >
                {roots.map((root) => (
                  <button
                    type="button"
                    role="option"
                    aria-selected={root.qname === selected?.qname}
                    className={root.qname === selected?.qname ? "active" : ""}
                    key={root.qname}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => selectRoot(root)}
                  >
                    <span>
                      {root.type === "Table"
                        ? root.name || objectName(root.qname)
                        : root.name}
                    </span>
                    <small>
                      {root.type} · {root.schema}
                    </small>
                  </button>
                ))}
              </nav>
            ) : null}
          </div>
        </div>
        {detailOpen ? (
          <DetailsPanel
            selection={selection}
            detail={detail}
            detailState={detailState}
            flow={flow}
            onClose={() => setDetailOpen(false)}
            onRetry={() => selection?.type === "node" && loadNodeDetail(selection.item.id)}
          />
        ) : (
          <button type="button" className="open-detail" onClick={() => setDetailOpen(true)}>Open details</button>
        )}
      </aside>
      <button
        type="button"
        className="detail-resizer"
        aria-label="Resize detail panel"
        title="Use Left/Right arrows to resize"
        onPointerDown={startDetailResize}
        onKeyDown={resizeDetailWithKeyboard}
      />
      <main className="main">
        {flowState.status === "loading" ? <div className="state-banner" role="status">Loading relationships…</div> : null}
        {flowState.status === "error" ? <div className="state-banner error" role="alert">{flowState.error} <button onClick={() => selectRoot(selected)}>Retry</button></div> : null}
        {flowState.truncated ? <div className="state-banner warning" role="status">Graph truncated at the server limit. Narrow direction or relation filters.</div> : null}
        {flowState.status === "ready" && flow.edges.length === 0 ? <div className="state-banner" role="status">No visible one-hop relationships. Use 1/W/R/E or change filters.</div> : null}
        <section className="content-grid graph-only">
          <GraphCard
            key={selected?.nodeId || "empty"}
            flow={flow}
            focusNodeId={focusNodeId}
            resetKey={selected?.nodeId || ""}
            selection={selection}
            setFlow={setFlow}
            setFocusNodeId={setFocusNodeId}
            setSelection={(next) => { setSelection(next); setDetailOpen(true); }}
            loadNodeDetail={loadNodeDetail}
            contract={contract}
          />
        </section>
      </main>
    </div>
  );
}

function toGraph(center, flows, contract, centerPosition = { x: 720, y: 360 }) {
  const nodes = new Map();
  const add = (qualified_name, label, name, position, display = null, style = null) =>
    nodes.set(qualified_name, {
      id: qualified_name,
      type: "flowNode",
      position,
      data: {
        type: label || "Table",
        label: display?.title || name || shortName(qualified_name),
        subtitle: display?.subtitle || null,
        scope: display?.scope || null,
        display,
        qname: qualified_name,
        style: style || contract.nodes?.[label] || center.style || {},
        code: "",
      },
    });
  const centerX = centerPosition.x;
  const centerY = centerPosition.y;
  add(center.qualified_name, center.label, center.name, {
    x: centerX,
    y: centerY,
  }, center.display, center.style);
  flows.forEach((row, i) => {
    const otherQname =
      row.from_qname === center.qualified_name ? row.to_qname : row.from_qname;
    const otherLabel =
      row.from_qname === center.qualified_name ? row.to_label : row.from_label;
    const otherName =
      row.from_qname === center.qualified_name ? row.to_name : row.from_name;
    const left = row.to_qname === center.qualified_name;
    const columnX = left ? 120 : 1320;
    const sameSideIndex = flows
      .slice(0, i)
      .filter((item) =>
        left
          ? item.to_qname === center.qualified_name
          : item.from_qname === center.qualified_name,
      ).length;
    const sameSideTotal = flows.filter((item) =>
      left
        ? item.to_qname === center.qualified_name
        : item.from_qname === center.qualified_name,
    ).length;
    const rowGap = 190;
    const startY = centerY - ((sameSideTotal - 1) * rowGap) / 2;
    const otherDisplay = row.from_qname === center.qualified_name ? row.to_display : row.from_display;
    add(otherQname, otherLabel, otherName, {
      x: columnX,
      y: startY + sameSideIndex * rowGap,
    }, otherDisplay);
  });
  const edges = flows.map((row) => {
    const flowType = row.flow_type || row.rel_type;
    const edgeStyle = row.style || contract.edges?.[flowType] || {};
    const color = edgeStyle.color || flowColors[flowType] || "#94a3b8";
    return {
      id: edgeId(row),
      source: row.from_qname,
      target: row.to_qname,
      type: "lane",
      label: flowType,
      data: { ...row, baseline: true },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color,
      },
      style: { stroke: color, strokeWidth: 2.2, strokeDasharray: "5 6" },
      interactionWidth: 18,
      labelStyle: { fill: "#e5e7eb", fontSize: 10, fontWeight: 800 },
      labelBgPadding: [7, 4],
      labelBgBorderRadius: 4,
      labelBgStyle: { fill: "#0f172a", fillOpacity: 0.92 },
    };
  });
  return { nodes: [...nodes.values()], edges };
}

function edgeId(row) {
  return [
    row.from_qname,
    row.to_qname,
    row.rel_type,
    row.line || "",
    row.source_file || "",
  ].join("|");
}

function expandGraph(flow, center, flows, contract) {
  const centerNode = flow.nodes.find(
    (node) => node.id === center.qualified_name,
  );
  const position = centerNode?.position || { x: 720, y: 360 };
  const expanded = toGraph(center, flows, contract, position);
  const nodes = new Map(flow.nodes.map((node) => [node.id, node]));
  const edges = new Map(flow.edges.map((edge) => [edge.id, edge]));
  expanded.nodes.forEach((node) => {
    if (!nodes.has(node.id)) nodes.set(node.id, node);
  });
  expanded.edges.forEach((edge) => edges.set(edge.id, edge));
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

function expandInPlace(flow, center, flows, contract) {
  const centerNode = flow.nodes.find(
    (node) => node.id === center.qualified_name,
  );
  const centerPosition = centerNode?.position || { x: 720, y: 360 };
  const expanded = toGraph(center, flows, contract, centerPosition);
  const nodes = new Map(flow.nodes.map((node) => [node.id, node]));
  const edges = new Map(flow.edges.map((edge) => [edge.id, edge]));
  const occupied = [...nodes.values()].map((node) => node.position);
  const newNodes = expanded.nodes.filter((node) => !nodes.has(node.id));
  newNodes.forEach((node, index) => {
    const side = node.position.x < centerPosition.x ? -1 : 1;
    const preferred = {
      x: centerPosition.x + side * 480,
      y: centerPosition.y + (index - (newNodes.length - 1) / 2) * 220,
    };
    const position = findFreeSlot(preferred, occupied);
    occupied.push(position);
    nodes.set(node.id, { ...node, position });
  });
  expanded.edges.forEach((edge) => edges.set(edge.id, edge));
  return {
    nodes: [...nodes.values()],
    edges: spaceEdgeLabels([...edges.values()]),
  };
}

function mergeFlowRows(flow, center, flows, contract, loadKey) {
  const centerNode = flow.nodes.find(
    (node) => node.id === center.qualified_name,
  );
  const centerPosition = centerNode?.position || { x: 720, y: 360 };
  const nodes = new Map(flow.nodes.map((node) => [node.id, node]));
  const edges = new Map(flow.edges.map((edge) => [edge.id, edge]));
  const occupied = [...nodes.values()].map((node) => node.position);
  const newNodeCount = { value: 0 };
  const ensureNode = (qname, label, name, sideHint = 1, display = null, style = null) => {
    const existing = nodes.get(qname);
    if (existing) {
      if (loadKey && Array.isArray(existing.data?.loadKeys)) {
        nodes.set(qname, {
          ...existing,
          data: {
            ...existing.data,
            loadKeys: [...new Set([...existing.data.loadKeys, loadKey])],
          },
        });
      }
      return;
    }
    const preferred = {
      x: centerPosition.x + sideHint * 480,
      y: centerPosition.y + newNodeCount.value * 190,
    };
    const position = findFreeSlot(preferred, occupied);
    occupied.push(position);
    newNodeCount.value += 1;
    nodes.set(qname, {
      id: qname,
      type: "flowNode",
      position,
      data: {
        type: label || "Table",
        label: display?.title || name || shortName(qname),
        subtitle: display?.subtitle || null,
        scope: display?.scope || null,
        display,
        qname,
        style: style || contract.nodes?.[label] || {},
        code: "",
        loadKeys: [loadKey],
      },
    });
  };
  ensureNode(center.qualified_name, center.label, center.name, 0, center.display, center.style);
  flows.forEach((row) => {
    const sourceSide = row.to_qname === center.qualified_name ? -1 : 1;
    const targetSide = row.from_qname === center.qualified_name ? 1 : -1;
    ensureNode(row.from_qname, row.from_label, row.from_name, sourceSide, row.from_display, row.from_style);
    ensureNode(row.to_qname, row.to_label, row.to_name, targetSide, row.to_display, row.to_style);
    const flowType = row.flow_type || row.rel_type;
    const edgeStyle = row.style || contract.edges?.[flowType] || {};
    const color = edgeStyle.color || flowColors[flowType] || "#94a3b8";
    const id = edgeId(row);
    const existing = edges.get(id);
    edges.set(id, {
      ...(existing || {}),
      id,
      source: row.from_qname,
      target: row.to_qname,
      type: "lane",
      label: flowType,
      data: {
        ...row,
        baseline: Boolean(existing?.data?.baseline),
        loadKeys: [...new Set([...(existing?.data?.loadKeys || []), loadKey])],
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color,
      },
      style: { stroke: color, strokeWidth: 2.2, strokeDasharray: "5 6" },
      interactionWidth: 18,
      labelStyle: { fill: "#e5e7eb", fontSize: 10, fontWeight: 800 },
      labelBgPadding: [7, 4],
      labelBgBorderRadius: 4,
      labelBgStyle: { fill: "#0f172a", fillOpacity: 0.92 },
    });
  });
  return {
    nodes: [...nodes.values()],
    edges: spaceEdgeLabels([...edges.values()]),
  };
}

async function loadFlowRows(rootId, direction, selectedTypes, recursive, signal) {
  const endpoint = recursive ? "/api/flow-all" : "/api/flow";
  return api(
    `${endpoint}?qname=${encodeURIComponent(rootId)}&direction=${direction}&types=${encodeURIComponent(selectedTypes.join(","))}`,
    { signal },
  );
}

function filterVisibleEdges(edges, nodes) {
  const filtersByNode = new Map(
    nodes.map((node) => [node.id, node.data?.edgeFilters || {}]),
  );
  return edges.filter((edge) => {
    const loadKeys = edge.data?.loadKeys || [];
    const belongsToRecursiveFlow = loadKeys.some((key) => key.endsWith("|all"));
    if (belongsToRecursiveFlow) return true;
    const type = edge.data?.flow_type || edge.data?.rel_type || edge.label;
    const sourceOut = filtersByNode.get(edge.source)?.out || EDGE_TYPE_DEFAULTS;
    const targetIn = filtersByNode.get(edge.target)?.in || EDGE_TYPE_DEFAULTS;
    return sourceOut.includes(type) && targetIn.includes(type);
  });
}

function filterVisibleNodes(nodes, visibleEdges, keepNodeIds = []) {
  const connectedNodeIds = new Set();
  visibleEdges.forEach((edge) => {
    connectedNodeIds.add(edge.source);
    connectedNodeIds.add(edge.target);
  });
  const preservedNodeIds = new Set(keepNodeIds.filter(Boolean));
  if (!preservedNodeIds.size && nodes[0]?.id) preservedNodeIds.add(nodes[0].id);
  return nodes.filter(
    (node) =>
      !Array.isArray(node.data?.loadKeys) ||
      preservedNodeIds.has(node.id) ||
      connectedNodeIds.has(node.id),
  );
}

function unloadRelationGroup(flow, loadKey, keepNodeId) {
  const edges = flow.edges
    .map((edge) => {
      const loadKeys = edge.data?.loadKeys;
      if (!loadKeys?.includes(loadKey)) return edge;
      const nextKeys = loadKeys.filter((key) => key !== loadKey);
      return nextKeys.length || edge.data?.baseline
        ? { ...edge, data: { ...edge.data, loadKeys: nextKeys } }
        : null;
    })
    .filter(Boolean);
  const connectedNodeIds = new Set([keepNodeId]);
  edges.forEach((edge) => {
    connectedNodeIds.add(edge.source);
    connectedNodeIds.add(edge.target);
  });
  const nodes = flow.nodes
    .map((node) => {
      const loadKeys = node.data?.loadKeys;
      if (!loadKeys?.includes(loadKey)) return node;
      const nextKeys = loadKeys.filter((key) => key !== loadKey);
      return { ...node, data: { ...node.data, loadKeys: nextKeys } };
    })
    .filter(
      (node) =>
        node &&
        (!node.data?.loadKeys ||
          node.data.loadKeys.length ||
          connectedNodeIds.has(node.id)),
    );
  return { nodes, edges: spaceEdgeLabels(edges) };
}

function findFreeSlot(preferred, occupied) {
  const gapX = 280;
  const gapY = 180;
  for (let ring = 0; ring < 12; ring += 1) {
    const candidates = ring
      ? [
          { x: preferred.x, y: preferred.y + ring * gapY },
          { x: preferred.x, y: preferred.y - ring * gapY },
          { x: preferred.x + ring * gapX, y: preferred.y },
          { x: preferred.x - ring * gapX, y: preferred.y },
        ]
      : [preferred];
    const found = candidates.find(
      (item) => !occupied.some((pos) => overlaps(item, pos)),
    );
    if (found) return found;
  }
  return { x: preferred.x, y: preferred.y + occupied.length * gapY };
}

function overlaps(a, b) {
  return Math.abs(a.x - b.x) < 280 && Math.abs(a.y - b.y) < 180;
}

function applyStableLayout(flow, centerId) {
  const levels = layoutLevels(flow, centerId);
  const nodeIds = [...levels.keys()];
  const ordered = (ids) =>
    [...ids].sort((a, b) => objectName(a).localeCompare(objectName(b)));
  const placeColumn = (ids, x, gap = 240) => {
    const rows = ordered(ids);
    const startY = -((rows.length - 1) * gap) / 2;
    return rows.map((id, i) => [id, { x, y: startY + i * gap }]);
  };
  const columnX = (id) => {
    const level = levels.get(id) ?? 0;
    return 620 + level * 520;
  };
  const rawPositions = new Map();
  [...new Set(levels.values())]
    .sort((a, b) => a - b)
    .forEach((level) => {
      const ids = nodeIds.filter((id) => levels.get(id) === level);
      placeColumn(ids, 620 + level * 520).forEach(([id, pos]) =>
        rawPositions.set(id, pos),
      );
    });
  const positions = resolveOverlaps(rawPositions, nodeIds, columnX);
  return {
    nodes: flow.nodes.map((node) => ({
      ...node,
      position: positions.get(node.id) || node.position,
    })),
    edges: spaceEdgeLabels(flow.edges),
  };
}

function layoutLevels(flow, centerId) {
  const nodeIds = flow.nodes.map((node) => node.id);
  const incoming = new Map(nodeIds.map((id) => [id, []]));
  const outgoing = new Map(nodeIds.map((id) => [id, []]));
  flow.edges.forEach((edge) => {
    if (!incoming.has(edge.target) || !outgoing.has(edge.source)) return;
    incoming.get(edge.target).push(edge.source);
    outgoing.get(edge.source).push(edge.target);
  });
  const levels = new Map([[centerId, 0]]);
  walkLevels(incoming, levels, centerId, -1);
  walkLevels(outgoing, levels, centerId, 1);
  nodeIds.filter((id) => !levels.has(id)).forEach((id) => levels.set(id, 0));
  return levels;
}

function walkLevels(links, levels, startId, step) {
  const queue = [[startId, 0]];
  while (queue.length) {
    const [id, level] = queue.shift();
    (links.get(id) || []).forEach((nextId) => {
      const nextLevel = level + step;
      if (
        levels.has(nextId) &&
        Math.abs(levels.get(nextId)) <= Math.abs(nextLevel)
      )
        return;
      levels.set(nextId, nextLevel);
      queue.push([nextId, nextLevel]);
    });
  }
}

function spaceEdgeLabels(edges) {
  const groups = new Map();
  edges.forEach((edge) => {
    const key = `${edge.source}|${edge.target}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(edge);
  });
  return edges.map((edge) => {
    const group = groups.get(`${edge.source}|${edge.target}`) || [edge];
    const index = group.findIndex((item) => item.id === edge.id);
    const offset = (index - (group.length - 1) / 2) * 58;
    return {
      ...edge,
      type: "lane",
      data: { ...edge.data, laneOffset: offset },
    };
  });
}

function resolveOverlaps(positions, nodeIds, columnX) {
  const minGap = 220;
  const grouped = new Map();
  nodeIds.forEach((id) => {
    const x = columnX(id);
    if (!grouped.has(x)) grouped.set(x, []);
    grouped.get(x).push(id);
  });
  const next = new Map(positions);
  grouped.forEach((ids, x) => {
    const rows = ids
      .map((id) => [id, next.get(id)?.y ?? 0])
      .sort(
        (a, b) =>
          a[1] - b[1] || objectName(a[0]).localeCompare(objectName(b[0])),
      );
    let y = rows[0]?.[1] ?? 0;
    rows.forEach(([id], i) => {
      if (i) y = Math.max(y + minGap, next.get(id)?.y ?? 0);
      next.set(id, { x, y });
    });
    const shift = ((rows.length - 1) * minGap) / 2;
    rows.forEach(([id]) => next.set(id, { x, y: next.get(id).y - shift }));
  });
  return next;
}

function DefinitionCard({ table, columns }) {
  return (
    <article className="card definition-card">
      <h3>Table Definition</h3>
      <DefinitionTable table={table} columns={columns} />
    </article>
  );
}

function DefinitionTable({ table = {}, columns = [], showSummary = true }) {
  return (
    <div className="definition-table-wrap">
      {showSummary ? <TableSummary table={table} /> : null}
      <div className="columns-scroll">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Column code</TableHead>
              <TableHead>Japanese name</TableHead>
              <TableHead>English name</TableHead>
              <TableHead>Description</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {columns.length ? (
              columns.map((row) => (
                <TableRow key={row.id || row.code || row.name}>
                  <TableCell className="font-mono text-xs text-sky-300">
                    {row.code || row.name}
                  </TableCell>
                  <TableCell>{row.name_ja || "-"}</TableCell>
                  <TableCell>{row.name_en || "-"}</TableCell>
                  <TableCell className="text-slate-400">
                    {row.description || "-"}
                  </TableCell>
                </TableRow>
              ))
            ) : (
              <TableRow>
                <TableCell
                  colSpan="4"
                  className="h-24 text-center text-slate-500"
                >
                  No column metadata
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

function TableSummary({ table = {} }) {
  return (
    <div className="table-summary">
      <strong>{table.code || "-"}</strong>
      <span>JP: {table.name_ja || "-"}</span>
      <span>EN: {table.name_en || "-"}</span>
      {table.description ? <p>{table.description}</p> : null}
    </div>
  );
}

function GraphCard({
  flow,
  focusNodeId,
  resetKey,
  selection,
  setFlow,
  setFocusNodeId,
  setSelection,
  loadNodeDetail,
  contract,
}) {
  const expansionAbortRef = useRef(null);
  const [loadedRelationKeys, setLoadedRelationKeys] = useState(() => new Set());
  const [expansionState, setExpansionState] = useState({ status: "idle", error: "", truncated: false });
  useEffect(() => {
    setLoadedRelationKeys(new Set());
    setExpansionState({ status: "idle", error: "", truncated: false });
    return () => expansionAbortRef.current?.abort();
  }, [resetKey]);

  const loadRelations = async (node, direction, edgeTypes, options = {}) => {
    const selectedTypes = Array.isArray(edgeTypes)
      ? edgeTypes
      : EDGE_TYPE_DEFAULTS;
    const recursive = options.recursive !== false;
    if (!selectedTypes.length) return;
    const scope = recursive ? "all" : "one";
    const loadKey = `${node.id}|${direction}|${[...selectedTypes].sort().join(",")}|${scope}`;
    if (loadedRelationKeys.has(loadKey)) {
      setFocusNodeId(node.id);
      loadNodeDetail(node.id);
      return;
    }
    const controller = new AbortController();
    expansionAbortRef.current?.abort();
    expansionAbortRef.current = controller;
    setExpansionState({ status: "loading", error: "", truncated: false });
    try {
      const data = await loadFlowRows(node.id, direction, selectedTypes, recursive, controller.signal);
      const rows = data.flows || [];
      let nextSelection = null;
      setFlow((current) => {
        const graph = mergeFlowRows(
          current,
          {
            label: node.data.type,
            qualified_name: node.id,
            name: node.data.label,
          },
          rows,
          contract,
          loadKey,
        );
        nextSelection =
          graph.nodes.find((item) => item.id === node.id) || graph.nodes[0];
        return graph;
      });
      if (nextSelection) setSelection({ type: "node", item: nextSelection });
      setLoadedRelationKeys((current) => new Set(current).add(loadKey));
      setFocusNodeId(node.id);
      loadNodeDetail(node.id);
      setExpansionState({ status: "ready", error: "", truncated: Boolean(data.truncated) });
    } catch (error) {
      if (error.name !== "AbortError") {
        setExpansionState({ status: "error", error: error.message, truncated: false });
      }
    }
  };
  const unloadRelations = (node, direction, edgeTypes, options = {}) => {
    const selectedTypes = Array.isArray(edgeTypes)
      ? edgeTypes
      : EDGE_TYPE_DEFAULTS;
    const recursive = options.recursive !== false;
    const scope = recursive ? "all" : "one";
    const loadKey = `${node.id}|${direction}|${[...selectedTypes].sort().join(",")}|${scope}`;
    setFlow((current) => unloadRelationGroup(current, loadKey, node.id));
    setLoadedRelationKeys((current) => {
      const next = new Set(current);
      next.delete(loadKey);
      return next;
    });
  };
  const changeNodeEdgeFilters = (nodeId, edgeTypes) => {
    setFlow((current) => ({
      ...current,
      nodes: current.nodes.map((node) =>
        node.id === nodeId
          ? { ...node, data: { ...node.data, edgeFilters: edgeTypes } }
          : node,
      ),
    }));
  };
  const changeNodeEdgeTypes = (nodeId, edgeTypes, changedDirection) => {
    const changedDirections = changedDirection
      ? [changedDirection]
      : ["in", "out"];
    const keysToUnload = [...loadedRelationKeys].filter((key) =>
      changedDirections.some((direction) =>
        key.startsWith(`${nodeId}|${direction}|`),
      ),
    );
    if (keysToUnload.length) {
      setFlow((current) =>
        keysToUnload.reduce(
          (nextFlow, key) => unloadRelationGroup(nextFlow, key, nodeId),
          current,
        ),
      );
      setLoadedRelationKeys((current) => {
        const next = new Set(current);
        keysToUnload.forEach((key) => next.delete(key));
        return next;
      });
    }
    setFlow((current) => ({
      ...current,
      nodes: current.nodes.map((node) =>
        node.id === nodeId
          ? { ...node, data: { ...node.data, edgeFilters: edgeTypes } }
          : node,
      ),
    }));
  };
  const graphNodes = useMemo(
    () =>
      flow.nodes.map((node) => ({
        ...node,
        data: {
          ...node.data,
          edgeFilters: node.data.edgeFilters || {
            in: EDGE_TYPE_DEFAULTS,
            out: EDGE_TYPE_DEFAULTS,
          },
          onLoadRelations: loadRelations,
          onUnloadRelations: unloadRelations,
          onSetEdgeFilters: changeNodeEdgeFilters,
          onChangeEdgeTypes: changeNodeEdgeTypes,
          loadedRelationKeys,
        },
      })),
    [flow.nodes, flow, contract, loadedRelationKeys],
  );
  const visibleEdges = useMemo(
    () => filterVisibleEdges(flow.edges, graphNodes),
    [flow.edges, graphNodes],
  );
  const selectedNodeId =
    selection?.type === "node" ? selection.item?.id : undefined;
  const visibleNodes = useMemo(
    () =>
      filterVisibleNodes(graphNodes, visibleEdges, [
        resetKey,
        focusNodeId,
        selectedNodeId,
      ]),
    [graphNodes, visibleEdges, resetKey, focusNodeId, selectedNodeId],
  );
  return (
    <ReactFlowProvider>
      <article className="card graph-card full">
        <FlowViewportController focusNodeId={focusNodeId} nodes={visibleNodes} />
        {expansionState.status === "loading" ? <p className="state-banner graph-state" role="status">Loading expanded relationships…</p> : null}
        {expansionState.status === "error" ? <p className="state-banner error graph-state" role="alert">{expansionState.error}</p> : null}
        {expansionState.truncated ? <p className="state-banner warning graph-state" role="status">Recursive graph truncated at the server limit.</p> : null}
        <div className="card-title-row">
          <h3>Table Relationship</h3>
          <GraphTools
            flow={flow}
            setFlow={setFlow}
            selection={selection}
            setFocusNodeId={setFocusNodeId}
          />
        </div>
        <div className="graph-body">
          <div className="flow-wrap">
            <ReactFlow
              nodes={visibleNodes}
              edges={visibleEdges}
              nodeTypes={rfNodeTypes}
              edgeTypes={edgeTypes}
              fitView
              proOptions={{ hideAttribution: true }}
              nodesDraggable
              nodesConnectable={false}
              onNodesChange={(changes) =>
                setFlow((current) => ({
                  ...current,
                  nodes: applyNodeChanges(changes, current.nodes),
                }))
              }
              onNodeClick={(_, node) => {
                setSelection({ type: "node", item: node });
                loadNodeDetail(node.id);
              }}
              onEdgeClick={(_, edge) =>
                setSelection({ type: "edge", item: edge })
              }
              panOnDrag
              zoomOnScroll
            >
              <Background color="transparent" />
            </ReactFlow>
          </div>
        </div>
        <div className="legend">
          {Object.entries(contract.edges || {})
            .filter(([, item]) => item.visible !== false)
            .map(([type, item]) => (
              <span key={type}>
                <span
                  className="swatch"
                  style={{
                    background: item.color || flowColors[type] || "#94a3b8",
                  }}
                />{" "}
                {type}
              </span>
            ))}
        </div>
      </article>
    </ReactFlowProvider>
  );
}

function FlowViewportController({ focusNodeId, nodes }) {
  const { fitView } = useReactFlow();
  const [didFit, setDidFit] = useState(false);
  useEffect(() => {
    if (!focusNodeId || !nodes.length || didFit) return;
    requestAnimationFrame(() =>
      fitView({
        padding: 0.22,
        duration: 450,
      }),
    );
    setDidFit(true);
  }, [focusNodeId, nodes, fitView, didFit]);
  return null;
}

function GraphTools({ flow, setFlow, selection, setFocusNodeId }) {
  const { zoomIn, zoomOut, fitView } = useReactFlow();
  const autoLayout = () => {
    const centerId =
      selection?.type === "node" && selection.item?.id
        ? selection.item.id
        : flow.nodes[0]?.id;
    if (!centerId) return;
    setFlow(applyStableLayout(flow, centerId));
    setFocusNodeId(centerId);
    requestAnimationFrame(() => fitView({ padding: 0.22, duration: 450 }));
  };
  return (
    <div className="tool-buttons" aria-label="Graph tools">
      <button onClick={autoLayout} aria-label="Auto layout" title="Auto layout">
        Auto layout
      </button>
      <button onClick={() => zoomIn()} aria-label="Zoom in">
        <Plus size={15} />
      </button>
      <button onClick={() => zoomOut()} aria-label="Zoom out">
        <Minus size={15} />
      </button>
      <button onClick={() => fitView()} aria-label="Fit view">
        <Maximize2 size={15} />
      </button>
      <button aria-label="Locked">
        <Lock size={14} />
      </button>
    </div>
  );
}

function DetailsPanel({ selection, detail, detailState, flow, onClose, onRetry }) {
  const item = selection?.item;
  if (!item)
    return (
      <aside className="details-panel">
        <PanelHeader title="Details" onClose={onClose} />
        <h4>Select a node or edge to view metadata, relationships, and evidence.</h4>
      </aside>
    );
  if (selection.type === "edge") {
    const edge = item.data || {};
    const relationKey = `${edge.from_qname}|${edge.to_qname}|${edge.rel_type}`;
    return <OccurrenceEdgeDetails key={relationKey} edge={edge} onClose={onClose} />;
  }
  const isTable = item.data.type === "Table";
  const table = detail?.table || { code: item.data.name };
  const columns = detail?.columns || [];
  const visibleEdges = filterVisibleEdges(flow?.edges || [], flow?.nodes || []);
  const nodeEdges = visibleEdges.filter(
    (edge) => edge.source === item.id || edge.target === item.id,
  );
  return (
    <aside className="details-panel">
      <PanelHeader title={item.data.label || "Details"} onClose={onClose} />
      {detailState?.status === "loading" ? <p className="state-banner" role="status">Loading details…</p> : null}
      {detailState?.status === "error" ? <p className="state-banner error" role="alert">{detailState.error} <button onClick={onRetry}>Retry</button></p> : null}
      {detail?.warnings?.map((warning) => <p className="state-banner warning" role="status" key={warning.code}>{warning.message}</p>)}
      {isTable ? (
        <DetailSection title="Table definition">
          <TableSummary table={table} />
        </DetailSection>
      ) : (
        <DetailSection title={`${item.data.type} detail`}>
          <dl>
            <dt>Name</dt><dd>{detail?.node?.display?.title || item.data.label}</dd>
            {Object.entries(detail?.properties || {}).map(([key, value]) => (
              <React.Fragment key={key}><dt>{humanize(key)}</dt><dd>{formatDetailValue(value)}</dd></React.Fragment>
            ))}
            <dt>Contributing sources</dt><dd>{detail?.counts?.sources ?? "—"}</dd>
            <dt>Total relations</dt><dd>{detail?.counts?.relations ?? nodeEdges.length}</dd>
            <dt>Occurrences</dt><dd>{detail?.counts?.occurrences ?? "—"}</dd>
          </dl>
        </DetailSection>
      )}
      {isTable ? (
        <DetailSection title="Columns">
          <DefinitionTable columns={columns} showSummary={false} />
        </DetailSection>
      ) : null}
      {detail?.sources?.length ? <SourceList sources={detail.sources} /> : null}
      <RelationSummary node={item} edges={nodeEdges} total={detail?.counts?.relations} />
      {detail?.impact?.columns?.length ? (
        <DetailSection title="Column impact">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Column</TableHead>
                <TableHead>Read</TableHead>
                <TableHead>Insert</TableHead>
                <TableHead>Update</TableHead>
                <TableHead>Delete</TableHead>
                <TableHead>Merge</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {detail.impact.columns.map((row) => (
                <TableRow key={row.name}>
                  <TableCell className="font-mono text-xs text-sky-300">
                    {row.name}
                  </TableCell>
                  <TableCell>{actorNames(row.reads)}</TableCell>
                  <TableCell>{actorNames(row.inserts)}</TableCell>
                  <TableCell>{actorNames(row.updates)}</TableCell>
                  <TableCell>{actorNames(row.deletes)}</TableCell>
                  <TableCell>{actorNames(row.merges)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </DetailSection>
      ) : null}
      <DebugMetadata
        items={[
          ["Type", item.data.type],
          ["Qname", item.data.qname],
          ["Schema", schemaName(item.data.qname) || "—"],
        ]}
      />
    </aside>
  );
}

function OccurrenceEdgeDetails({ edge, onClose }) {
  const [evidence, setEvidence] = useState(edge.evidence || []);
  const [selectedId, setSelectedId] = useState(edge.evidence?.[0]?.occurrence_id || "");
  const [nextCursor, setNextCursor] = useState(edge.next_cursor || null);
  const [pageState, setPageState] = useState({ status: "idle", error: "" });
  const [snippet, setSnippet] = useState(null);
  const [snippetState, setSnippetState] = useState({ status: "idle", error: "" });
  const relationQuery = useMemo(() => new URLSearchParams({
    from_qname: edge.from_qname || "", to_qname: edge.to_qname || "", rel_type: edge.rel_type || "",
  }).toString(), [edge.from_qname, edge.to_qname, edge.rel_type]);
  useEffect(() => {
    const initial = edge.evidence || [];
    setEvidence(initial);
    setSelectedId(initial[0]?.occurrence_id || "");
    setNextCursor(edge.next_cursor || null);
    setPageState({ status: "idle", error: "" });
  }, [relationQuery, edge.evidence, edge.next_cursor]);
  const occurrence = evidence.find((item) => item.occurrence_id === selectedId) || evidence[0];
  useEffect(() => {
    setSnippet(null);
    if (!occurrence?.occurrence_id) {
      setSnippetState({ status: "empty", error: "No source occurrence available." });
      return undefined;
    }
    const controller = new AbortController();
    setSnippetState({ status: "loading", error: "" });
    api(`/api/snippet?${relationQuery}&occurrence_id=${encodeURIComponent(occurrence.occurrence_id)}`, { signal: controller.signal })
      .then((data) => {
        setSnippet(data);
        setSnippetState({ status: data.status === "available" ? "ready" : "empty", error: data.status === "available" ? "" : `Source ${data.status}.` });
      })
      .catch((error) => {
        if (error.name !== "AbortError") setSnippetState({ status: "error", error: error.message });
      });
    return () => controller.abort();
  }, [relationQuery, occurrence?.occurrence_id]);
  const loadMore = async () => {
    if (!nextCursor) return;
    setPageState({ status: "loading", error: "" });
    try {
      const page = await api(`/api/edge-evidence?${relationQuery}&cursor=${encodeURIComponent(nextCursor)}&limit=50`);
      setEvidence((items) => [...items, ...(page.evidence || [])]);
      setNextCursor(page.next_cursor || null);
      setPageState({ status: "ready", error: "" });
    } catch (error) {
      setPageState({ status: "error", error: error.message });
    }
  };
  const fromName = edge.from_display?.title || edge.from_name || objectName(edge.from_qname);
  const toName = edge.to_display?.title || edge.to_name || objectName(edge.to_qname);
  const occurrenceIndex = Math.max(0, evidence.findIndex((item) => item.occurrence_id === occurrence?.occurrence_id));
  const columns = occurrence?.columns?.length ? occurrence.columns : edge.columns || [];
  return (
    <aside className="details-panel">
      <PanelHeader title={`${edge.operation || edge.rel_type || "Relation"}: ${fromName} → ${toName}`} onClose={onClose} />
      <DetailSection title="Flow summary"><div className="edge-summary"><strong>{flowDescription(edge.flow_type)}</strong><p><span>{fromName}</span><b>→</b><span>{toName}</span></p></div></DetailSection>
      <DetailSection title={`Occurrence ${evidence.length ? occurrenceIndex + 1 : 0} / ${edge.evidence_count ?? evidence.length}`}>
        {evidence.length ? <label className="occurrence-select">Source occurrence<select aria-label="Select source occurrence" value={occurrence?.occurrence_id || ""} onChange={(event) => setSelectedId(event.target.value)}>{evidence.map((item, index) => <option key={item.occurrence_id} value={item.occurrence_id}>{index + 1}. {item.source_path}:{item.line || "?"}</option>)}</select></label> : <p>No source occurrences.</p>}
        {nextCursor ? <button type="button" onClick={loadMore} disabled={pageState.status === "loading"}>{pageState.status === "loading" ? "Loading…" : "Load more occurrences"}</button> : null}
        {pageState.status === "error" ? <p className="state-banner error" role="alert">{pageState.error}</p> : null}
        {edge.evidence_truncated && nextCursor ? <p className="state-banner warning">More evidence available; load the next page.</p> : null}
      </DetailSection>
      {occurrence ? <DetailSection title="Occurrence metadata"><dl>{[
        ["Source", occurrence.source_path], ["Line", occurrence.line], ["Operation", occurrence.operation || edge.operation],
        ["Query ID", occurrence.query_id], ["Mapper tag", occurrence.mapper_tag], ["Call type", occurrence.call_type],
        ["Expression", occurrence.expression], ["Extractor", occurrence.extractor_name], ["Confidence", occurrence.confidence],
      ].filter(([, value]) => value != null && value !== "").map(([label, value]) => <React.Fragment key={label}><dt>{label}</dt><dd>{formatDetailValue(value)}</dd></React.Fragment>)}</dl>{columns.length ? <div className="column-tags">{columns.map((column) => <span key={column}>{column}</span>)}</div> : null}</DetailSection> : null}
      <DetailSection title="Source snippet">
        {snippetState.status === "loading" ? <p role="status">Loading source…</p> : null}
        {["empty", "error"].includes(snippetState.status) ? <p className="state-banner error" role="alert">{snippetState.error}</p> : null}
        {snippet?.status === "available" ? <SourceSnippet snippet={snippet} /> : null}
      </DetailSection>
      <DebugMetadata items={[["Raw relation", edge.rel_type], ["Flow type", edge.flow_type], ["Source qname", edge.from_qname], ["Target qname", edge.to_qname]]} />
    </aside>
  );
}

function SourceSnippet({ snippet }) {
  return <div className="source-snippet"><header>{snippet.source_path}:{snippet.focus_line} · {snippet.language || "source"}</header><pre>{String(snippet.text || "").split("\n").map((line, index) => {
    const lineNumber = snippet.start_line + index;
    return <span key={lineNumber} className={lineNumber === snippet.focus_line ? "focus-line" : ""}><b>{lineNumber}</b>{line}{"\n"}</span>;
  })}</pre></div>;
}

function PanelHeader({ title, onClose }) {
  return <header className="panel-header"><h4>{title}</h4><button type="button" aria-label="Close detail panel" onClick={onClose}>Close</button></header>;
}

function SourceList({ sources }) {
  return <DetailSection title="Sources"><ul className="source-list">{sources.map((source) => (
    <li key={`${source.source_id}:${source.source_path}`}><strong>{source.source_path}</strong><span>{source.project || "No project"} · {source.extractor || "Unknown extractor"} · {source.availability}</span></li>
  ))}</ul></DetailSection>;
}

const humanize = (key) => key.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
const formatDetailValue = (value) => Array.isArray(value) ? value.join(", ") || "—" : typeof value === "boolean" ? (value ? "Yes" : "No") : value ?? "—";

function InsightCard({ title, children }) {
  return <DetailSection title={title}>{children}</DetailSection>;
}

function DetailSection({ title, children, defaultOpen = true }) {
  return (
    <details className="debug-metadata detail-section" open={defaultOpen}>
      <summary>
        <span>{title}</span>
      </summary>
      <div className="detail-section-body">{children}</div>
    </details>
  );
}

function DebugMetadata({ items = [] }) {
  return (
    <details className="debug-metadata">
      <summary>
        <span>Debug metadata</span>
      </summary>
      <dl>
        {items.map(([label, value]) => (
          <React.Fragment key={label}>
            <dt>{label}</dt>
            <dd>{value || "—"}</dd>
          </React.Fragment>
        ))}
      </dl>
    </details>
  );
}

function RelationSummary({ node, edges, total }) {
  if (!edges.length) return null;
  const rows = edges.map((edge) => relationRow(node, edge));
  return (
    <DetailSection title={`Visible relationships (${edges.length} of ${total ?? edges.length})`}>
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead>Meaning</TableHead>
            <TableHead>Columns</TableHead>
            <TableHead>Evidence</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={row.id}>
              <TableCell>{row.meaning}</TableCell>
              <TableCell>{row.columns}</TableCell>
              <TableCell>{row.evidence}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </DetailSection>
  );
}

function relationRow(node, edge) {
  const data = edge.data || {};
  const type = data.flow_type || data.rel_type || edge.label;
  const incoming = edge.target === node.id;
  const otherName = incoming
    ? data.from_name || objectName(data.from_qname || edge.source)
    : data.to_name || objectName(data.to_qname || edge.target);
  const currentName = node.data?.label || objectName(node.id);
  const meaning = relationMeaning(type, incoming, currentName, otherName);
  const columns = data.columns || [];
  const evidence =
    data.source_file || data.line
      ? `${data.source_file || "source"}${data.line ? `:${data.line}` : ""}`
      : "—";
  return {
    id: edge.id,
    meaning,
    columns: columns.length ? columns.join(", ") : "—",
    evidence,
  };
}

function flowDescription(type = "") {
  const normalized = String(type).toUpperCase();
  const descriptions = {
    READS: "Reads data from",
    REMOTE_READS: "Reads remote data from",
    WRITES: "Writes data to",
    DERIVES: "Derives/calculates data for",
    CALLS: "Calls",
    TRIGGERS: "Triggers",
    USES: "Uses",
  };
  return descriptions[normalized] || `Relation: ${type || "Unknown"}`;
}

function relationMeaning(type = "", incoming, currentName, otherName) {
  const normalized = String(type).toUpperCase();
  if (incoming) {
    const meanings = {
      READS: `${otherName} reads data from ${currentName}.`,
      REMOTE_READS: `${otherName} reads remote data from ${currentName}.`,
      WRITES: `${otherName} writes data into ${currentName}.`,
      DERIVES: `${otherName} calculates or derives values for ${currentName}.`,
      CALLS: `${otherName} calls ${currentName}.`,
      TRIGGERS: `${otherName} triggers ${currentName}.`,
      USES: `${otherName} uses ${currentName}.`,
    };
    return (
      meanings[normalized] || `${otherName} has a relation to ${currentName}.`
    );
  }
  const meanings = {
    READS: `${currentName} reads data from ${otherName}.`,
    REMOTE_READS: `${currentName} reads remote data from ${otherName}.`,
    WRITES: `${currentName} writes data into ${otherName}.`,
    DERIVES: `${currentName} calculates or derives values for ${otherName}.`,
    CALLS: `${currentName} calls ${otherName}.`,
    TRIGGERS: `${currentName} triggers ${otherName}.`,
    USES: `${currentName} uses ${otherName}.`,
  };
  return (
    meanings[normalized] || `${currentName} has a relation to ${otherName}.`
  );
}

function edgeImpactText(type = "", fromName, toName, columns = []) {
  const normalized = String(type).toUpperCase();
  const columnText = columns.length
    ? ` Columns involved: ${columns.join(", ")}.`
    : "";
  const impacts = {
    READS: `Changes in ${toName} data or columns can affect logic/results in ${fromName}.${columnText}`,
    REMOTE_READS: `Changes in remote source ${toName} can affect logic/results in ${fromName}.${columnText}`,
    WRITES: `Changes in ${fromName} can change data persisted in ${toName}.${columnText}`,
    DERIVES: `Calculation changes in ${fromName} can change derived values for ${toName}.${columnText}`,
    CALLS: `Changes in called logic ${toName} can affect execution of ${fromName}.`,
    TRIGGERS: `Changes in ${fromName} can trigger behavior in ${toName}.`,
    USES: `${fromName} depends on ${toName}; changes to ${toName} may affect this flow.`,
  };
  return (
    impacts[normalized] || `${fromName} is related to ${toName}.${columnText}`
  );
}

function actorNames(items = []) {
  return items.length
    ? [...new Set(items.map((item) => item.node))].join(", ")
    : "—";
}

function LaneEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  markerEnd,
  style,
  label,
  data,
}) {
  const offset = data?.laneOffset || 0;
  const distance = Math.max(Math.abs(targetX - sourceX), 180);
  const control = distance * 0.45;
  const labelX = (sourceX + targetX) / 2;
  const labelY = (sourceY + targetY) / 2;
  const path = `M ${sourceX},${sourceY} C ${sourceX + control},${sourceY + offset} ${targetX - control},${targetY + offset} ${targetX},${targetY}`;
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        style={style}
        interactionWidth={18}
      />
      <EdgeLabelRenderer>
        <div
          className="edge-label"
          style={{
            transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
          }}
        >
          {label}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

function FlowNode({ data }) {
  const [openDirection, setOpenDirection] = useState(null);
  const nodeRef = useRef(null);
  const meta = data.style || nodeTypes[data.type] || nodeTypes.Table;
  const Icon = icons[meta.icon] || nodeTypes[data.type]?.icon || Box;
  const id = data.label || objectName(data.qname);
  const schema = data.scope || schemaName(data.qname);
  const isTable = data.type === "Table";
  const tableParts = isTable ? tableDisplayParts(data.label, data.qname) : null;
  const edgeFilters = data.edgeFilters || {
    in: EDGE_TYPE_DEFAULTS,
    out: EDGE_TYPE_DEFAULTS,
  };
  const relationKey = (direction, types, recursive = true) =>
    `${data.qname}|${direction}|${[...types].sort().join(",")}|${recursive ? "all" : "one"}`;
  const isReadLoaded = (direction) =>
    data.loadedRelationKeys?.has(relationKey(direction, READ_FLOW_TYPES));
  const isWriteLoaded = (direction) =>
    data.loadedRelationKeys?.has(relationKey(direction, WRITE_FLOW_TYPES));
  const isEveryLoaded = (direction) =>
    data.loadedRelationKeys?.has(relationKey(direction, EVERY_FLOW_TYPES));
  const isOneHopLoaded = (direction) =>
    data.loadedRelationKeys?.has(
      relationKey(direction, EDGE_TYPE_DEFAULTS, false),
    );
  const stop = (event) => event.stopPropagation();
  const toggleOneHop = (direction) => (event) => {
    event.stopPropagation();
    const checked = event.target.checked;
    const payload = { id: data.qname, data };
    if (checked) {
      data.onLoadRelations?.(payload, direction, EDGE_TYPE_DEFAULTS, {
        recursive: false,
      });
      setOpenDirection(direction);
    } else {
      data.onUnloadRelations?.(payload, direction, EDGE_TYPE_DEFAULTS, {
        recursive: false,
      });
      setOpenDirection(null);
    }
  };
  const changeFilter = (direction, nextTypes) => {
    data.onSetEdgeFilters?.(data.qname, {
      ...edgeFilters,
      [direction]: nextTypes,
    });
  };
  const toggleType = (direction, type) => (event) => {
    event.stopPropagation();
    const current = edgeFilters[direction] || EDGE_TYPE_DEFAULTS;
    const next = current.includes(type)
      ? current.filter((item) => item !== type)
      : [...current, type];
    changeFilter(direction, next);
  };
  const toggleAll = (direction) => (event) => {
    event.stopPropagation();
    const current = edgeFilters[direction] || EDGE_TYPE_DEFAULTS;
    changeFilter(
      direction,
      current.length === EDGE_TYPE_DEFAULTS.length ? [] : EDGE_TYPE_DEFAULTS,
    );
  };
  const toggleLoadFlowTypes = (direction, types) => (event) => {
    event.stopPropagation();
    const checked = event.target.checked;
    const availableTypes = types.filter(
      (type) => type === "REMOTE_READS" || EDGE_TYPE_DEFAULTS.includes(type),
    );
    if (!availableTypes.length) return;
    const payload = { id: data.qname, data };
    if (checked) {
      data.onLoadRelations?.(payload, direction, availableTypes, {
        recursive: true,
      });
    } else {
      data.onUnloadRelations?.(payload, direction, availableTypes, {
        recursive: true,
      });
    }
  };
  useEffect(() => {
    if (!openDirection) return undefined;
    const closeOnOutsideClick = (event) => {
      if (nodeRef.current?.contains(event.target)) return;
      setOpenDirection(null);
    };
    document.addEventListener("pointerdown", closeOnOutsideClick);
    return () =>
      document.removeEventListener("pointerdown", closeOnOutsideClick);
  }, [openDirection]);
  return (
    <div
      ref={nodeRef}
      className={`flow-node ${meta.className}`}
      title={data.label}
    >
      <Handle className="flow-handle" type="target" position={Position.Left} />
      <Handle className="flow-handle" type="source" position={Position.Right} />
      <NodeFlowControls
        direction="in"
        oneHopChecked={isOneHopLoaded("in")}
        writeChecked={isWriteLoaded("in")}
        readChecked={isReadLoaded("in")}
        everyChecked={isEveryLoaded("in")}
        onToggleOneHop={toggleOneHop("in")}
        onToggleWrite={toggleLoadFlowTypes("in", WRITE_FLOW_TYPES)}
        onToggleRead={toggleLoadFlowTypes("in", READ_FLOW_TYPES)}
        onToggleEvery={toggleLoadFlowTypes("in", EVERY_FLOW_TYPES)}
      />
      <NodeFlowControls
        direction="out"
        oneHopChecked={isOneHopLoaded("out")}
        writeChecked={isWriteLoaded("out")}
        readChecked={isReadLoaded("out")}
        everyChecked={isEveryLoaded("out")}
        onToggleOneHop={toggleOneHop("out")}
        onToggleWrite={toggleLoadFlowTypes("out", WRITE_FLOW_TYPES)}
        onToggleRead={toggleLoadFlowTypes("out", READ_FLOW_TYPES)}
        onToggleEvery={toggleLoadFlowTypes("out", EVERY_FLOW_TYPES)}
      />
      {openDirection ? (
        <div
          className={`node-edge-popover ${openDirection}`}
          onClick={stop}
          onMouseDown={stop}
        >
          <EdgeDirectionFilter
            title={openDirection === "in" ? "Incoming" : "Outgoing"}
            direction={openDirection}
            selected={edgeFilters[openDirection] || EDGE_TYPE_DEFAULTS}
            onToggleAll={toggleAll}
            onToggleType={toggleType}
          />
        </div>
      ) : null}
      <div className="node-head">
        <div className="node-icon">
          <Icon size={14} />
        </div>
        <strong>{data.type}</strong>
      </div>
      <div className="node-id">{isTable ? tableParts.code : id}</div>
      {isTable ? (
        <div className="node-meta">
          <span>JP: {tableParts.nameJa}</span>
          <span>EN: {tableParts.nameEn}</span>
        </div>
      ) : data.subtitle ? <div className="node-meta"><span>{data.subtitle}</span></div> : null}
      <div className="node-foot">
        <span>{schema || "-"}</span>
        <span>{data.type}</span>
      </div>
    </div>
  );
}

function NodeFlowControls({
  direction,
  oneHopChecked,
  writeChecked,
  readChecked,
  everyChecked,
  onToggleOneHop,
  onToggleWrite,
  onToggleRead,
  onToggleEvery,
}) {
  const title = direction === "in" ? "Incoming" : "Outgoing";
  return (
    <div
      className={`node-flow-controls ${direction}`}
      onClick={(event) => event.stopPropagation()}
    >
      <label title={`Load one-hop ${title} edges using the edge filter`}>
        <input
          type="checkbox"
          aria-label={`Load one-hop ${title} edges`}
          checked={Boolean(oneHopChecked)}
          onChange={onToggleOneHop}
        />
        1
      </label>
      <label title={`Load all ${title} Write flow`}>
        <input
          type="checkbox"
          aria-label={`Load all ${title} write flow`}
          checked={Boolean(writeChecked)}
          onChange={onToggleWrite}
        />
        W
      </label>
      <label title={`Load all ${title} Read flow`}>
        <input
          type="checkbox"
          aria-label={`Load all ${title} read flow`}
          checked={Boolean(readChecked)}
          onChange={onToggleRead}
        />
        R
      </label>
      <label title={`Load all ${title} edges recursively`}>
        <input
          type="checkbox"
          aria-label={`Load every ${title} relation recursively`}
          checked={Boolean(everyChecked)}
          onChange={onToggleEvery}
        />
        E
      </label>
    </div>
  );
}

function EdgeDirectionFilter({
  title,
  direction,
  selected,
  onToggleAll,
  onToggleType,
}) {
  const allSelected = EDGE_TYPE_DEFAULTS.every((type) =>
    selected.includes(type),
  );
  return (
    <div className="edge-filter-group">
      <div className="edge-filter-title">{title}</div>
      <div className="edge-filter-section-title">Show edges</div>
      <label className="edge-filter-all">
        <input
          type="checkbox"
          checked={allSelected}
          onChange={onToggleAll(direction)}
        />
        All edges
      </label>
      <div className="edge-filter-options">
        {EDGE_TYPE_DEFAULTS.map((type) => (
          <label key={`${direction}-${type}`}>
            <input
              type="checkbox"
              checked={selected.includes(type)}
              onChange={onToggleType(direction, type)}
            />
            {type}
          </label>
        ))}
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
