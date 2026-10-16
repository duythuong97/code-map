import React from "react";
import { Handle, Position } from "@xyflow/react";
import { Box, Code2, Database, FileCode2, GitBranch, Layers3, Zap } from "lucide-react";
import { expansionIntentKey, groupLoadState, isExternalNode } from "../flow-state.js";
import { directionLabels, flowKey, modeLabels, modes, nodeTypeKinds } from "./constants.js";
import { formatLabel, identityParts } from "./format.js";

function flowNodeKind(nodeType = "") {
  return nodeTypeKinds[nodeType] || "detail";
}

const nodeIcons = {
  table: Database,
  code: Code2,
  trigger: Zap,
  file: FileCode2,
  sequence: GitBranch,
  "source-file": FileCode2,
  repository: Layers3,
  application: Box,
};

export function NodeFlowControls({ node, direction, data }) {
  if (!node?.node_id || !data?.onToggleFlow) return null;
  return <div className={`node-flow-controls nodrag ${direction}`} aria-label={`${directionLabels[direction]} node flow controls`} onClick={(event) => event.stopPropagation()} onMouseDown={(event) => event.stopPropagation()}>
    {modes.map((mode) => {
      const key = flowKey(node.node_id, mode, direction, data.semantic, data.filters);
      const state = groupLoadState(data.loadStates, key);
      const active = Boolean(data.intents?.[expansionIntentKey(node.node_id, mode, direction)] || data.groups?.[key]);
      const title = [directionLabels[direction], modeLabels[mode], state.loading ? "loading" : "", state.error || "", state.truncated ? "truncated" : ""].filter(Boolean).join(" · ");
      return <label key={key} className={[active ? "active" : "", state.loading ? "loading" : "", state.error ? "error" : "", state.truncated ? "truncated" : ""].filter(Boolean).join(" ")} title={title}>
        <input aria-label={`${directionLabels[direction]} ${modeLabels[mode]}`} type="checkbox" checked={active} onChange={(event) => data.onToggleFlow(node, mode, direction, event.target.checked)} />
        {mode}
      </label>;
    })}
  </div>;
}

export function FlowNode({ data }) {
  const node = data.node;
  const kind = flowNodeKind(node.node_type);
  const NodeIcon = nodeIcons[kind] || Box;
  const identity = identityParts(node, data.localized);
  return <div className={`flow-node ${kind} ${isExternalNode(node) ? "external" : ""}`}>
    <Handle type="target" position={Position.Left} className="flow-handle" />
    <Handle type="source" position={Position.Right} className="flow-handle" />
    <NodeFlowControls node={node} direction="in" data={data} />
    <NodeFlowControls node={node} direction="out" data={data} />
    <div className="node-head">
      <NodeIcon className="node-type-icon" size={18} aria-hidden="true" />
      <strong>{formatLabel(node.node_type)}</strong>
    </div>
    <div className="node-identity">
      <span className="node-code" title={identity.code}><small>Mã:</small><strong>{identity.code}</strong></span>
      <span lang="ja" title={identity.ja}><small>JA:</small>{identity.ja || "—"}</span>
      <span lang="en" title={identity.en}><small>EN:</small>{identity.en || "—"}</span>
    </div>
  </div>;
}

export const nodeTypes = { flowNode: FlowNode };
