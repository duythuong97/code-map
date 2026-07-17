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
  Info,
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
const EDGE_TYPE_DEFAULTS = ["CALLS", "READS", "WRITES", "DERIVES", "TRIGGERS", "USES"];

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
};
const rfNodeTypes = { flowNode: FlowNode };
const edgeTypes = { lane: LaneEdge };
const basePath = window.CODE_MAP_CONFIG?.urlPrefix || "";
const api = (path) => fetch(`${basePath}${path}`).then((res) => res.json());
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
  name: row.name || shortName(row.qualified_name),
  type: row.label,
  schema: row.qualified_name?.split(":").pop()?.split(".")[0] || "",
  qname: row.qualified_name,
  nodeId: row.qualified_name,
});

function App() {
  const resizeStartRef = useRef(null);
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [schemas, setSchemas] = useState([]);
  const [schema, setSchema] = useState("");
  const [roots, setRoots] = useState([]);
  const [selected, setSelected] = useState(null);
  const [flow, setFlow] = useState({ nodes: [], edges: [] });
  const [focusNodeId, setFocusNodeId] = useState("");
  const [selection, setSelection] = useState(null);
  const [detail, setDetail] = useState(null);
  const [contract, setContract] = useState({ nodes: nodeTypes, edges: {} });
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

  useEffect(() => {
    api("/api/schemas").then((rows) =>
      setSchemas(Array.isArray(rows) ? rows : []),
    );
    api("/api/graph-contract").then((data) =>
      setContract({ nodes: data.nodes || nodeTypes, edges: data.edges || {} }),
    );
  }, []);

  useEffect(() => {
    const t = setTimeout(() => {
      const term = query.trim();
      if (!term) {
        setRoots([]);
        return;
      }
      const schemaArg = schema ? `&schema=${encodeURIComponent(schema)}` : "";
      api(`/api/search?q=${encodeURIComponent(term)}${schemaArg}`).then(
        (rows) => setRoots(Array.isArray(rows) ? rows.map(rootView) : []),
      );
    }, 180);
    return () => clearTimeout(t);
  }, [query, schema]);

  function selectRoot(root) {
    if (!root) return;
    setQuery(root.name || objectName(root.qname));
    setSearchOpen(false);
    setSelected(root);
    setFocusNodeId(root.nodeId);
    api(`/api/flow?qname=${encodeURIComponent(root.qname)}`).then((data) => {
      const graph = toGraph(
        data.node || {
          label: root.type,
          qualified_name: root.qname,
          name: root.name,
        },
        [],
        data.contract || contract,
      );
      const layouted = applyStableLayout(graph, root.qname);
      setFlow(layouted);
      setSelection({
        type: "node",
        item:
          layouted.nodes.find((node) => node.id === root.qname) ||
          layouted.nodes[0],
      });
      api(`/api/node-detail?qname=${encodeURIComponent(root.qname)}`).then(
        setDetail,
      );
    });
  }

  async function submitSearch() {
    const rawTerm = query.trim();
    const term = rawTerm.toLowerCase();
    if (!term) return;
    const schemaArg = schema ? `&schema=${encodeURIComponent(schema)}` : "";
    const rows = await api(
      `/api/search?q=${encodeURIComponent(rawTerm)}${schemaArg}`,
    );
    const matches = Array.isArray(rows) ? rows.map(rootView) : roots;
    setRoots(matches);
    const exact = matches.find((root) => {
      const values = [root.name, objectName(root.qname), root.qname]
        .filter(Boolean)
        .map((value) => value.toLowerCase());
      return values.includes(term);
    });
    selectRoot(exact || matches[0]);
  }

  return (
    <div
      className="app-shell dark"
      style={{ "--detail-width": `${detailWidth}px` }}
    >
      <aside className="sidebar">
        <h1></h1>
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
                placeholder="Search table / package / procedure..."
              />
            </label>
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
        <DetailsPanel selection={selection} detail={detail} flow={flow} />
      </aside>
      <button
        type="button"
        className="detail-resizer"
        aria-label="Resize detail panel"
        onPointerDown={startDetailResize}
      />
      <main className="main">
        <section className="content-grid graph-only">
          <GraphCard
            flow={flow}
            focusNodeId={focusNodeId}
            selection={selection}
            setFlow={setFlow}
            setFocusNodeId={setFocusNodeId}
            setSelection={setSelection}
            setDetail={setDetail}
            contract={contract}
          />
        </section>
      </main>
    </div>
  );
}

function toGraph(center, flows, contract, centerPosition = { x: 720, y: 360 }) {
  const nodes = new Map();
  const add = (qualified_name, label, name, position) =>
    nodes.set(qualified_name, {
      id: qualified_name,
      type: "flowNode",
      position,
      data: {
        type: label || "Table",
        label: name || shortName(qualified_name),
        qname: qualified_name,
        style: contract.nodes?.[label] || center.style || {},
        code: "",
      },
    });
  const centerX = centerPosition.x;
  const centerY = centerPosition.y;
  add(center.qualified_name, center.label, center.name, {
    x: centerX,
    y: centerY,
  });
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
    add(otherQname, otherLabel, otherName, {
      x: columnX,
      y: startY + sameSideIndex * rowGap,
    });
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
      data: row,
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

function unloadRelationGroup(flow, loadKey, keepNodeId) {
  const edges = flow.edges
    .map((edge) => {
      const loadKeys = edge.data?.loadKeys;
      if (!loadKeys?.includes(loadKey)) return edge;
      const nextKeys = loadKeys.filter((key) => key !== loadKey);
      return nextKeys.length
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
    .filter((node) => node && (!node.data?.loadKeys || node.data.loadKeys.length || connectedNodeIds.has(node.id)));
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
  nodeIds
    .filter((id) => !levels.has(id))
    .forEach((id) => levels.set(id, 0));
  return levels;
}

function walkLevels(links, levels, startId, step) {
  const queue = [[startId, 0]];
  while (queue.length) {
    const [id, level] = queue.shift();
    (links.get(id) || []).forEach((nextId) => {
      const nextLevel = level + step;
      if (levels.has(nextId) && Math.abs(levels.get(nextId)) <= Math.abs(nextLevel)) return;
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
  selection,
  setFlow,
  setFocusNodeId,
  setSelection,
  setDetail,
  contract,
}) {
  const [loadedRelationKeys, setLoadedRelationKeys] = useState(() => new Set());
  const loadRelations = (node, direction, edgeTypes) => {
    const selectedTypes = Array.isArray(edgeTypes) ? edgeTypes : EDGE_TYPE_DEFAULTS;
    if (!selectedTypes.length) return;
    const types = selectedTypes.join(",");
    const loadKey = `${node.id}|${direction}|${[...selectedTypes].sort().join(",")}`;
    if (loadedRelationKeys.has(loadKey)) {
      setFlow((current) => unloadRelationGroup(current, loadKey, node.id));
      setLoadedRelationKeys((current) => {
        const next = new Set(current);
        next.delete(loadKey);
        return next;
      });
      setFocusNodeId(node.id);
      return;
    }
    const path = `/api/flow?qname=${encodeURIComponent(node.id)}&direction=${direction}&types=${encodeURIComponent(types)}`;
    api(path).then((data) => {
      const loadedEdgeIds = new Set((data.flows || []).map(edgeId));
      let nextSelection = null;
      setFlow((current) => {
        const existingNodeIds = new Set(current.nodes.map((item) => item.id));
        const existingEdgeIds = new Set(current.edges.map((item) => item.id));
        const graph = expandInPlace(
          current,
          data.node || {
            label: node.data.type,
            qualified_name: node.id,
            name: node.data.label,
          },
          data.flows || [],
          data.contract || contract,
        );
        const taggedGraph = {
          nodes: graph.nodes.map((item) =>
            existingNodeIds.has(item.id)
              ? item
              : {
                  ...item,
                  data: {
                    ...item.data,
                    loadKeys: [...(item.data.loadKeys || []), loadKey],
                  },
                },
          ),
          edges: graph.edges.map((item) =>
            loadedEdgeIds.has(item.id) && !existingEdgeIds.has(item.id)
              ? {
                  ...item,
                  data: {
                    ...item.data,
                    loadKeys: [...new Set([...(item.data.loadKeys || []), loadKey])],
                  },
                }
              : item,
          ),
        };
        nextSelection = taggedGraph.nodes.find((item) => item.id === node.id) || taggedGraph.nodes[0];
        return taggedGraph;
      });
      if (nextSelection) {
        setSelection({ type: "node", item: nextSelection });
      }
      setLoadedRelationKeys((current) => new Set(current).add(loadKey));
      setFocusNodeId(node.id);
      api(`/api/node-detail?qname=${encodeURIComponent(node.id)}`).then(setDetail);
    });
  };
  const changeNodeEdgeTypes = (nodeId, edgeTypes, changedDirection) => {
    const changedDirections = changedDirection ? [changedDirection] : ["in", "out"];
    const keysToUnload = [...loadedRelationKeys].filter((key) =>
      changedDirections.some((direction) => key.startsWith(`${nodeId}|${direction}|`)),
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
          onChangeEdgeTypes: changeNodeEdgeTypes,
          loadedRelationKeys,
        },
      })),
    [flow.nodes, flow, contract, loadedRelationKeys],
  );
  return (
    <ReactFlowProvider>
      <article className="card graph-card full">
        <FlowViewportController focusNodeId={focusNodeId} nodes={graphNodes} />
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
              nodes={graphNodes}
              edges={flow.edges}
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
                api(
                  `/api/node-detail?qname=${encodeURIComponent(node.id)}`,
                ).then(setDetail);
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

function GraphTools({
  flow,
  setFlow,
  selection,
  setFocusNodeId,
}) {
  const { zoomIn, zoomOut, fitView } = useReactFlow();
  const autoLayout = () => {
    const centerId =
      selection?.type === "node" ? selection.item.id : flow.nodes[0]?.id;
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

function DetailsPanel({ selection, detail, flow }) {
  if (!selection?.item)
    return (
      <aside className="details-panel">
        <h4>
          <Info size={15} /> Detail
        </h4>
      </aside>
    );
  const item = selection.item;
  if (selection.type === "edge") {
    const edge = item.data || {};
    const fromName = edge.from_name || objectName(edge.from_qname);
    const toName = edge.to_name || objectName(edge.to_qname);
    const type = edge.flow_type || edge.rel_type;
    const action = flowDescription(type);
    const columns = edge.columns?.length ? edge.columns : [];
    const meaning = relationMeaning(type, false, fromName, toName);
    const impact = edgeImpactText(type, fromName, toName, columns);
    return (
      <aside className="details-panel">
        <DetailSection title="Flow summary">
          <div className="edge-summary">
            <strong>{action}</strong>
            <p>
              <span>{fromName}</span>
              <b>→</b>
              <span>{toName}</span>
            </p>
          </div>
        </DetailSection>
        <div className="edge-insight-grid">
          <InsightCard title="Meaning">{meaning}</InsightCard>
          <InsightCard title="Affected columns">
            {columns.length ? (
              <div className="column-tags">
                {columns.map((column) => (
                  <span key={column}>{column}</span>
                ))}
              </div>
            ) : (
              "No column-level metadata for this relation."
            )}
          </InsightCard>
          <InsightCard title="Evidence">
            <p>
              {edge.source_file || "No source file"}
              {edge.line ? `:${edge.line}` : ""}
            </p>
            {edge.expression ? <p>Expression: {edge.expression}</p> : null}
            <pre>{edge.code || "No code evidence."}</pre>
          </InsightCard>
          <InsightCard title="Impact">{impact}</InsightCard>
        </div>
        <DebugMetadata
          items={[
            [
              "Relation",
              `${edge.flow_type || "—"}${
                edge.rel_type && edge.rel_type !== edge.flow_type
                  ? ` (${edge.rel_type})`
                  : ""
              }`,
            ],
            ["Source qname", edge.from_qname],
            ["Target qname", edge.to_qname],
            ["Confidence", edge.confidence || "—"],
          ]}
        />
      </aside>
    );
  }
  const isTable = item.data.type === "Table";
  const table = detail?.table || { code: item.data.name };
  const columns = detail?.columns || [];
  const nodeEdges = flow?.edges?.filter(
    (edge) => edge.source === item.id || edge.target === item.id,
  ) || [];
  return (
    <aside className="details-panel">
      {isTable ? (
        <DetailSection title="Table definition">
          <TableSummary table={table} />
        </DetailSection>
      ) : (
        <DetailSection title="Object detail">
          <dl>
            <dt>Name</dt>
            <dd>{item.data.label}</dd>
            <dt>Type</dt>
            <dd>{item.data.type}</dd>
            <dt>Code</dt>
            <dd>
              <pre>
                {detail?.code ||
                  item.data.code ||
                  "No code snippet. Re-extract to capture declaration lines."}
              </pre>
            </dd>
          </dl>
        </DetailSection>
      )}
      {isTable ? (
        <DetailSection title="Columns">
          <DefinitionTable columns={columns} showSummary={false} />
        </DetailSection>
      ) : null}
      <RelationSummary node={item} edges={nodeEdges} />
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

function InsightCard({ title, children }) {
  return (
    <DetailSection title={title}>{children}</DetailSection>
  );
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

function RelationSummary({ node, edges }) {
  if (!edges.length) return null;
  const rows = edges.map((edge) => relationRow(node, edge));
  return (
    <DetailSection title="Relationships">
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
  const evidence = data.source_file || data.line
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
    return meanings[normalized] || `${otherName} has a relation to ${currentName}.`;
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
  return meanings[normalized] || `${currentName} has a relation to ${otherName}.`;
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
  return impacts[normalized] || `${fromName} is related to ${toName}.${columnText}`;
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
  const id = objectName(data.qname);
  const schema = schemaName(data.qname);
  const isTable = data.type === "Table";
  const tableParts = isTable ? tableDisplayParts(data.label, data.qname) : null;
  const edgeFilters = data.edgeFilters || {
    in: EDGE_TYPE_DEFAULTS,
    out: EDGE_TYPE_DEFAULTS,
  };
  const relationKey = (direction) =>
    `${data.qname}|${direction}|${[...(edgeFilters[direction] || EDGE_TYPE_DEFAULTS)].sort().join(",")}`;
  const stop = (event) => event.stopPropagation();
  const openFilter = (direction) => (event) => {
    event.stopPropagation();
    setOpenDirection((current) => (current === direction ? null : direction));
  };
  const load = (direction) => (event) => {
    event.stopPropagation();
    data.onLoadRelations?.(
      { id: data.qname, data },
      direction,
      edgeFilters[direction] || EDGE_TYPE_DEFAULTS,
    );
    setOpenDirection(null);
  };
  const changeFilter = (direction, nextTypes) => {
    data.onChangeEdgeTypes?.(data.qname, {
      ...edgeFilters,
      [direction]: nextTypes,
    }, direction);
    if (nextTypes.length) {
      window.setTimeout(() => {
        data.onLoadRelations?.({ id: data.qname, data }, direction, nextTypes);
      }, 0);
    }
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
    changeFilter(direction, current.length === EDGE_TYPE_DEFAULTS.length ? [] : EDGE_TYPE_DEFAULTS);
  };
  useEffect(() => {
    if (!openDirection) return undefined;
    const closeOnOutsideClick = (event) => {
      if (nodeRef.current?.contains(event.target)) return;
      setOpenDirection(null);
    };
    document.addEventListener("pointerdown", closeOnOutsideClick);
    return () => document.removeEventListener("pointerdown", closeOnOutsideClick);
  }, [openDirection]);
  return (
    <div ref={nodeRef} className={`flow-node ${meta.className}`} title={data.label}>
      <Handle className="flow-handle" type="target" position={Position.Left} />
      <Handle className="flow-handle" type="source" position={Position.Right} />
      <button
        type="button"
        className={`node-relation-btn incoming ${data.loadedRelationKeys?.has(relationKey("in")) ? "active" : ""}`}
        title="Filter incoming relationships"
        onClick={openFilter("in")}
      >
        ←
      </button>
      <button
        type="button"
        className={`node-relation-btn outgoing ${data.loadedRelationKeys?.has(relationKey("out")) ? "active" : ""}`}
        title="Filter outgoing relationships"
        onClick={openFilter("out")}
      >
        →
      </button>
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
          <button
            type="button"
            className="edge-filter-load"
            onClick={load(openDirection)}
          >
            {data.loadedRelationKeys?.has(relationKey(openDirection)) ? "Unload" : "Load"}
          </button>
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
      ) : null}
      <div className="node-foot">
        <span>{schema || "-"}</span>
        <span>ID</span>
      </div>
    </div>
  );
}

function EdgeDirectionFilter({ title, direction, selected, onToggleAll, onToggleType }) {
  const allSelected = selected.length === EDGE_TYPE_DEFAULTS.length;
  return (
    <div className="edge-filter-group">
      <div className="edge-filter-title">{title}</div>
      <label className="edge-filter-all">
        <input type="checkbox" checked={allSelected} onChange={onToggleAll(direction)} />
        All
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
