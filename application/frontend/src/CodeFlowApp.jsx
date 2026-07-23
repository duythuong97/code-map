import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Background, Controls, MarkerType, ReactFlow } from "@xyflow/react";
import { LayoutGrid, Search } from "lucide-react";
import { api } from "./code-flow/api.js";
import {
  DEFAULT_DETAIL_WIDTH,
  DETAIL_WIDTH_STORAGE_KEY,
  MAX_DETAIL_WIDTH,
  MIN_DETAIL_WIDTH,
  clampDetailWidth,
  directionLabels,
  edgeColor,
  flowFiltersKey,
  flowKey,
  modeLabels,
} from "./code-flow/constants.js";
import DetailPanel from "./code-flow/DetailPanel.jsx";
import { EntityIdentity } from "./code-flow/detail-ui.jsx";
import { nodeTypes } from "./code-flow/FlowNode.jsx";
import {
  displayName,
  collectLocalizationIds,
  prioritizeSearchResults,
} from "./code-flow/format.js";
import { buildAutoLayout } from "./code-flow/layout.js";
import {
  isExternalNode,
  materializeGroups,
  mergeExpansionIntent,
  mergeGroup,
  removeExpansionIntent,
  removeGroup,
  removeGroupLoadState,
  setGroupLoadState,
} from "./flow-state.js";

export default function CodeFlowApp() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [searchOpen, setSearchOpen] = useState(false);
  const [browseResults, setBrowseResults] = useState([]);
  const [browseState, setBrowseState] = useState({ loading: false, error: "" });
  const [root, setRoot] = useState(null);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailHistory, setDetailHistory] = useState([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [searchState, setSearchState] = useState({ loading: false, error: "" });
  const [groups, setGroups] = useState({});
  const [groupStates, setGroupStates] = useState({});
  const [expansionIntents, setExpansionIntents] = useState({});
  const [semantic, setSemantic] = useState("APPLICATION");
  const [external, setExternal] = useState(true);
  const [maxNodes, setMaxNodes] = useState(500);
  const [layoutNonce, setLayoutNonce] = useState(0);
  const [manualNodePositions, setManualNodePositions] = useState({});
  const [notice, setNotice] = useState("");
  const [detailWidth, setDetailWidth] = useState(() => {
    if (typeof window === "undefined") return DEFAULT_DETAIL_WIDTH;
    const stored = Number(
      window.localStorage.getItem(DETAIL_WIDTH_STORAGE_KEY),
    );
    return clampDetailWidth(
      Number.isFinite(stored) && stored > 0 ? stored : DEFAULT_DETAIL_WIDTH,
    );
  });
  const [localized, setLocalized] = useState({});
  const expansionRequests = useRef(new Map());
  const searchRequest = useRef(null);
  const browseRequest = useRef(null);
  const sideRequest = useRef(null);
  const flowInstance = useRef(null);
  const searchArea = useRef(null);
  const skipNextSearch = useRef(false);
  const toggleNodeFlowRef = useRef(null);
  const stableToggleNodeFlow = useCallback(
    (...args) => toggleNodeFlowRef.current?.(...args),
    [],
  );
  const filtersKey = flowFiltersKey(maxNodes);

  function abortExpansion(key) {
    const controller = expansionRequests.current.get(key);
    if (controller) controller.abort();
    expansionRequests.current.delete(key);
  }

  function abortAllExpansions() {
    expansionRequests.current.forEach((controller) => controller.abort());
    expansionRequests.current.clear();
  }

  function abortSearch() {
    if (searchRequest.current) searchRequest.current.abort();
    searchRequest.current = null;
  }

  function abortBrowse() {
    if (browseRequest.current) browseRequest.current.abort();
    browseRequest.current = null;
  }

  function abortSide() {
    if (sideRequest.current) sideRequest.current.abort();
    sideRequest.current = null;
  }

  function abortAllRequests() {
    abortSearch();
    abortBrowse();
    abortSide();
    abortAllExpansions();
  }

  function resetExpandedGraph({ clearIntents = false } = {}) {
    abortAllExpansions();
    setGroups({});
    setGroupStates({});
    setManualNodePositions({});
    if (clearIntents) setExpansionIntents({});
  }

  function refreshExpandedGraph(
    nextSemantic = semantic,
    nextMaxNodes = maxNodes,
  ) {
    abortAllExpansions();
    setGroups({});
    setGroupStates({});
    setManualNodePositions({});
    const intents = Object.values(expansionIntents);
    if (!intents.length) return;
    setNotice(
      `Reloading ${intents.length} expansion${intents.length === 1 ? "" : "s"} for ${nextSemantic}…`,
    );
    intents.forEach((intent) => {
      void loadFlowGroup(
        intent.node,
        intent.mode,
        intent.direction,
        nextSemantic,
        nextMaxNodes,
      );
    });
  }

  function handleSemanticChange(nextSemantic) {
    if (nextSemantic === semantic) return;
    setSemantic(nextSemantic);
    refreshExpandedGraph(nextSemantic, maxNodes);
  }

  function handleMaxNodesChange(value) {
    const nextMaxNodes = Math.max(1, Math.min(1000, Number(value) || 1));
    setMaxNodes(nextMaxNodes);
    if (nextMaxNodes !== maxNodes) refreshExpandedGraph(semantic, nextMaxNodes);
  }

  async function loadBrowseRoots() {
    abortBrowse();
    const controller = new AbortController();
    browseRequest.current = controller;
    setBrowseState({ loading: true, error: "" });
    try {
      const rows = await api("/search?q=&exclude_node_types=COLUMN&limit=40", {
        signal: controller.signal,
      });
      if (browseRequest.current !== controller) return;
      setBrowseResults(Array.isArray(rows) ? rows : []);
      setBrowseState({ loading: false, error: "" });
    } catch (error) {
      if (error.name === "AbortError") return;
      if (browseRequest.current !== controller) return;
      setBrowseResults([]);
      setBrowseState({ loading: false, error: error.message });
      setNotice(error.message);
    } finally {
      if (browseRequest.current === controller) browseRequest.current = null;
    }
  }

  function openBrowseRoots(force = false) {
    setSearchOpen(true);
    if ((force || !query.trim()) && !browseState.loading)
      void loadBrowseRoots();
  }

  useEffect(() => {
    const term = query.trim();
    if (skipNextSearch.current) {
      skipNextSearch.current = false;
      abortSearch();
      setSearchState({ loading: false, error: "" });
      return undefined;
    }
    if (!term) {
      abortSearch();
      setResults([]);
      setSearchState({ loading: false, error: "" });
      return undefined;
    }
    const controller = new AbortController();
    abortSearch();
    searchRequest.current = controller;
    const timer = setTimeout(async () => {
      setSearchState({ loading: true, error: "" });
      try {
        const encodedTerm = encodeURIComponent(term);
        const rows = await api(
          `/search?q=${encodedTerm}&exclude_node_types=COLUMN&locale=ja&limit=40`,
          { signal: controller.signal },
        );
        if (searchRequest.current !== controller) return;
        setResults(
          prioritizeSearchResults(term, Array.isArray(rows) ? rows : []),
        );
        setSearchState({ loading: false, error: "" });
      } catch (error) {
        if (error.name === "AbortError") return;
        if (searchRequest.current !== controller) return;
        setResults([]);
        setSearchState({ loading: false, error: error.message });
        setNotice(error.message);
      }
    }, 400);
    return () => {
      clearTimeout(timer);
      controller.abort();
      if (searchRequest.current === controller) searchRequest.current = null;
    };
  }, [query]);

  useEffect(() => () => abortAllRequests(), []);

  useEffect(() => {
    const closeSearch = (event) => {
      if (!searchArea.current?.contains(event.target)) setSearchOpen(false);
    };
    document.addEventListener("pointerdown", closeSearch);
    return () => document.removeEventListener("pointerdown", closeSearch);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    window.localStorage.setItem(
      DETAIL_WIDTH_STORAGE_KEY,
      String(Math.round(detailWidth)),
    );
    const handleResize = () =>
      setDetailWidth((current) => clampDetailWidth(current));
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [detailWidth]);

  const graph = useMemo(() => materializeGroups(root, groups), [root, groups]);
  const localizationIds = useMemo(
    () => collectLocalizationIds(graph.nodes, results, browseResults, selected, detail),
    [graph.nodes, results, browseResults, selected, detail],
  );

  useEffect(() => {
    if (!localizationIds.length) {
      setLocalized({});
      return undefined;
    }
    let active = true;
    api(
      `/localization?${localizationIds.map((id) => `target_id=${encodeURIComponent(id)}`).join("&")}`,
    )
      .then((data) => {
        if (active) setLocalized((current) => ({ ...current, ...(data.items || {}) }));
      })
      .catch((error) => {
        if (active) setNotice(error.message);
      });
    return () => {
      active = false;
    };
  }, [localizationIds]);

  const { visibleNodes, visibleEdges } = useMemo(() => {
    const nodes = graph.nodes.filter(
      (node) => external || !isExternalNode(node),
    );
    const ids = new Set(nodes.map((node) => node.node_id));
    return {
      visibleNodes: nodes,
      visibleEdges: graph.edges.filter(
        (edge) => ids.has(edge.source_node_id) && ids.has(edge.target_node_id),
      ),
    };
  }, [graph, external]);
  const selectedNodeId = selected?.node_id;
  const autoLayoutNodes = useMemo(
    () =>
      buildAutoLayout(
        visibleNodes,
        visibleEdges,
        root?.node_id || selectedNodeId,
        localized,
      ),
    [visibleNodes, visibleEdges, root?.node_id, selectedNodeId, localized],
  );
  const flowNodes = useMemo(
    () =>
      autoLayoutNodes.map((node) => ({
        id: node.node_id,
        type: "flowNode",
        position: manualNodePositions[node.node_id] || node.position,
        data: {
          node,
          label: displayName(node, localized),
          localized,
          groups,
          loadStates: groupStates,
          intents: expansionIntents,
          onToggleFlow: stableToggleNodeFlow,
          semantic,
          filters: filtersKey,
        },
        className: [
          isExternalNode(node) ? "code-flow-external-node" : "",
          selectedNodeId === node.node_id ? "code-flow-selected-node" : "",
        ]
          .filter(Boolean)
          .join(" "),
      })),
    [
      autoLayoutNodes,
      manualNodePositions,
      localized,
      groups,
      groupStates,
      expansionIntents,
      stableToggleNodeFlow,
      semantic,
      filtersKey,
      selectedNodeId,
    ],
  );
  const flowEdges = useMemo(
    () =>
      visibleEdges.map((edge) => ({
        id: edge.edge_id,
        source: edge.source_node_id,
        target: edge.target_node_id,
        label: edge.edge_type,
        markerEnd: { type: MarkerType.ArrowClosed },
        style: { stroke: edgeColor(edge.edge_type), strokeWidth: 1.5 },
      })),
    [visibleEdges],
  );
  const topologySignature = useMemo(
    () =>
      `${root?.node_id || ""}|${semantic}|${external}|${filtersKey}|${flowNodes.map((node) => node.id).join(";")}|${flowEdges.map((edge) => edge.id).join(";")}`,
    [root?.node_id, semantic, external, filtersKey, flowNodes, flowEdges],
  );

  useEffect(() => {
    if (!flowNodes.length || !flowInstance.current) return undefined;
    const timer = window.setTimeout(() => {
      flowInstance.current?.fitView?.({ padding: 0.18, duration: 220 });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [topologySignature, layoutNonce]);

  function handleNodesChange(changes) {
    const positionChanges = (changes || []).filter(
      (change) =>
        change.type === "position" &&
        change.position &&
        change.dragging === false,
    );
    if (!positionChanges.length) return;
    setManualNodePositions((current) => {
      let changed = false;
      const next = { ...current };
      positionChanges.forEach((change) => {
        const previous = current[change.id];
        if (
          !previous ||
          previous.x !== change.position.x ||
          previous.y !== change.position.y
        ) {
          next[change.id] = { x: change.position.x, y: change.position.y };
          changed = true;
        }
      });
      return changed ? next : current;
    });
  }

  function runAutoLayout() {
    setManualNodePositions({});
    setLayoutNonce((current) => current + 1);
    setNotice(
      flowNodes.length
        ? `Auto layout applied: ${flowNodes.length} nodes`
        : "Select a node before applying auto layout",
    );
  }

  async function choose(node) {
    abortSearch();
    abortBrowse();
    resetExpandedGraph({ clearIntents: true });
    setRoot(node);
    setSelected({ ...node, targetType: "NODE" });
    setDetail(null);
    setDetailHistory([]);
    setDetailError("");
    setResults([]);
    setBrowseResults([]);
    setSearchOpen(false);
    skipNextSearch.current = true;
    setQuery(displayName(node, localized));
    setNotice("");
    await loadSide(node.node_id, "NODE");
  }

  async function loadSide(targetId, targetType) {
    abortSide();
    const controller = new AbortController();
    sideRequest.current = controller;
    setDetailLoading(true);
    setDetailError("");
    try {
      const detailPayload = await api(
        `/detail?target_id=${encodeURIComponent(targetId)}&target_type=${targetType}&locale=vi`,
        { signal: controller.signal },
      );
      if (sideRequest.current !== controller) return;
      setDetail(detailPayload);
    } catch (error) {
      if (error.name === "AbortError") return;
      if (sideRequest.current !== controller) return;
      setDetailError(error.message);
      setNotice(error.message);
    } finally {
      if (sideRequest.current === controller) {
        sideRequest.current = null;
        setDetailLoading(false);
      }
    }
  }

  async function loadFlowGroup(
    node,
    mode,
    direction = "both",
    semanticLevel = semantic,
    maxNodeValue = maxNodes,
  ) {
    if (!node?.node_id) return;
    if (!root) setRoot(node);
    const nextFiltersKey = flowFiltersKey(maxNodeValue);
    const key = flowKey(
      node.node_id,
      mode,
      direction,
      semanticLevel,
      nextFiltersKey,
    );
    abortExpansion(key);
    const controller = new AbortController();
    expansionRequests.current.set(key, controller);
    setGroupStates((current) =>
      setGroupLoadState(current, key, {
        loading: true,
        error: "",
        truncated: false,
      }),
    );
    setNotice(`Loading ${directionLabels[direction]} ${modeLabels[mode]}…`);
    try {
      const data = await api(
        `/flow?node_id=${encodeURIComponent(node.node_id)}&mode=${mode}&direction=${direction}&max_nodes=${maxNodeValue}&semantic_level=${semanticLevel}`,
        { signal: controller.signal },
      );
      if (expansionRequests.current.get(key) !== controller) return;
      setGroups((current) =>
        mergeGroup(current, key, {
          nodes: data.nodes || [],
          edges: data.edges || [],
        }),
      );
      setGroupStates((current) =>
        setGroupLoadState(current, key, {
          loading: false,
          error: "",
          truncated: Boolean(data.truncated),
        }),
      );
      setNotice(
        data.truncated
          ? `Truncated at max_nodes=${maxNodeValue}`
          : `${directionLabels[direction]} ${mode}: ${(data.nodes || []).length} nodes, ${(data.edges || []).length} edges`,
      );
    } catch (error) {
      if (error.name === "AbortError") return;
      setExpansionIntents((current) =>
        removeExpansionIntent(current, node.node_id, mode, direction),
      );
      setGroupStates((current) =>
        setGroupLoadState(current, key, {
          loading: false,
          error: error.message,
          truncated: false,
        }),
      );
      setNotice(error.message);
    } finally {
      if (expansionRequests.current.get(key) === controller)
        expansionRequests.current.delete(key);
    }
  }

  async function toggleNodeFlow(node, mode, direction = "both", checked) {
    if (!node?.node_id) return;
    if (!root) setRoot(node);
    const key = flowKey(node.node_id, mode, direction, semantic, filtersKey);
    if (!checked) {
      setExpansionIntents((current) =>
        removeExpansionIntent(current, node.node_id, mode, direction),
      );
      abortExpansion(key);
      setGroupStates((current) => removeGroupLoadState(current, key));
      return setGroups((current) => removeGroup(current, key));
    }
    setExpansionIntents((current) =>
      mergeExpansionIntent(current, node, mode, direction),
    );
    await loadFlowGroup(node, mode, direction, semantic, maxNodes);
  }
  toggleNodeFlowRef.current = toggleNodeFlow;

  async function selectElement(item) {
    if (!item?.node_id) return;
    setDetailHistory([]);
    setSelected({ ...item, targetType: "NODE" });
    setDetail(null);
    await loadSide(item.node_id, "NODE");
  }

  async function selectNodeById(nodeId) {
    if (!nodeId || nodeId === selected?.node_id) return;
    const node = graph.nodes.find((item) => item.node_id === nodeId) || {
      node_id: nodeId,
    };
    if (selected?.node_id) setDetailHistory((current) => [...current, selected]);
    setSelected({ ...node, targetType: "NODE" });
    setDetail(null);
    await loadSide(nodeId, "NODE");
  }

  async function backDetail() {
    const previous = detailHistory.at(-1);
    if (!previous?.node_id) return;
    setDetailHistory((current) => current.slice(0, -1));
    setSelected(previous);
    setDetail(null);
    await loadSide(previous.node_id, "NODE");
  }

  function startDetailResize(event) {
    if (event.button !== undefined && event.button !== 0) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = detailWidth;
    document.body.classList.add("resizing-detail");
    const handleMove = (moveEvent) =>
      setDetailWidth(clampDetailWidth(startWidth + moveEvent.clientX - startX));
    const stopResize = () => {
      document.body.classList.remove("resizing-detail");
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", stopResize);
      window.removeEventListener("pointercancel", stopResize);
    };
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", stopResize);
    window.addEventListener("pointercancel", stopResize);
  }

  function resizeDetailWithKeyboard(event) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    setDetailWidth((current) => {
      if (event.key === "Home") return MIN_DETAIL_WIDTH;
      if (event.key === "End") return clampDetailWidth(MAX_DETAIL_WIDTH);
      return clampDetailWidth(
        current + (event.key === "ArrowRight" ? 24 : -24),
      );
    });
  }

  const searchTerm = query.trim();
  const dropdownResults = searchTerm ? results : browseResults;
  const dropdownState = searchTerm ? searchState : browseState;

  return (
    <div
      className="code-flow-shell"
      style={{ "--code-flow-left-width": `${Math.round(detailWidth)}px` }}
    >
      <aside className="code-flow-left">
        <div className="code-flow-search-area" ref={searchArea}>
          <div className="code-flow-search">
            <Search size={16} />
            <input
              aria-label="Search tables"
              value={query}
              onFocus={() => openBrowseRoots()}
              onChange={(event) => {
                setSearchOpen(true);
                setQuery(event.target.value);
              }}
              placeholder="Search tables"
            />
            <button
              className="code-flow-search-browse"
              type="button"
              onClick={() => {
                setQuery("");
                openBrowseRoots(true);
              }}
            >
              Browse
            </button>
          </div>
          {searchOpen && dropdownResults.length ? (
            <div
              className="root-list"
              onMouseDown={(event) => event.preventDefault()}
            >
              {dropdownResults.map((node) => (
                <button
                  key={node.node_id}
                  className={root?.node_id === node.node_id ? "active" : ""}
                  type="button"
                  onClick={() => choose(node)}
                >
                  <EntityIdentity node={node} localized={localized} compact />
                  <small>{node.node_type}</small>
                </button>
              ))}
            </div>
          ) : null}
          {searchOpen &&
          !searchTerm &&
          !dropdownState.loading &&
          !dropdownResults.length ? (
            <p className="empty-search">Browse or type to search tables.</p>
          ) : null}
          {dropdownState.loading ? (
            <p className="code-flow-search-status">
              {searchTerm ? "Searching…" : "Loading tables…"}
            </p>
          ) : null}
          {dropdownState.error ? (
            <p className="code-flow-search-status error" role="alert">
              {dropdownState.error}
            </p>
          ) : null}
        </div>
        <DetailPanel
          selected={selected}
          detail={detail}
          canGoBack={detailHistory.length > 0}
          onBack={backDetail}
          onSelectNode={selectNodeById}
          localized={localized}
          loading={detailLoading}
          error={detailError}
        />
      </aside>
      <div
        className="detail-resizer"
        role="separator"
        aria-label="Resize detail panel"
        aria-orientation="vertical"
        aria-valuemin={MIN_DETAIL_WIDTH}
        aria-valuemax={Math.round(clampDetailWidth(MAX_DETAIL_WIDTH))}
        aria-valuenow={Math.round(detailWidth)}
        tabIndex={0}
        onPointerDown={startDetailResize}
        onKeyDown={resizeDetailWithKeyboard}
      />
      <main className="code-flow-main">
        <header className="code-flow-toolbar">
          <span role="status">{notice}</span>
        </header>
        <div className="code-flow-canvas" data-layout="auto-layered">
          <ReactFlow
            nodeTypes={nodeTypes}
            nodes={flowNodes}
            edges={flowEdges}
            nodesDraggable
            onlyRenderVisibleElements
            onNodesChange={handleNodesChange}
            fitView
            fitViewOptions={{ padding: 0.18 }}
            onInit={(instance) => {
              flowInstance.current = instance;
            }}
            onNodeClick={(_, node) =>
              selectElement(graph.nodes.find((item) => item.node_id === node.id))
            }
          >
            <Background />
            <Controls showInteractive={false}>
              <button
                className="react-flow__controls-button code-flow-auto-layout-control"
                type="button"
                disabled={!flowNodes.length}
                onClick={runAutoLayout}
                title="Auto layout"
                aria-label="Auto layout"
              >
                <LayoutGrid size={16} aria-hidden="true" />
              </button>
            </Controls>
          </ReactFlow>
        </div>
      </main>
    </div>
  );
}
