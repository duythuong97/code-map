export const modes = ["1", "W", "R", "E"];

export const flowFiltersKey = (maxNodes) => `max_nodes=${maxNodes}`;
export const flowKey = (nodeId, mode, direction = "both", semanticLevel = "TECHNICAL", filters = "") => `${nodeId}|${direction}|${mode}|${semanticLevel}|${filters}`;

export const LAYOUT_COLUMN_GAP = 460;
export const LAYOUT_ROW_GAP = 240;
export const LAYOUT_MARGIN = 64;

export const DETAIL_WIDTH_STORAGE_KEY = "code-flow-detail-width";
export const DEFAULT_DETAIL_WIDTH = 500;
export const MIN_DETAIL_WIDTH = 360;
export const MAX_DETAIL_WIDTH = 760;

export function clampDetailWidth(value) {
  const viewportLimit = typeof window === "undefined" ? MAX_DETAIL_WIDTH : Math.max(MIN_DETAIL_WIDTH, window.innerWidth - 420);
  return Math.min(Math.max(Number(value) || DEFAULT_DETAIL_WIDTH, MIN_DETAIL_WIDTH), Math.min(MAX_DETAIL_WIDTH, viewportLimit));
}

export const nodeTypeRank = {
  SCREEN: 10,
  UI_ACTION: 12,
  API_CALL_REFERENCE: 18,
  API_APPLICATION: 20,
  API_OPERATION: 22,
  CONTROLLER: 30,
  SERVICE: 32,
  REPOSITORY: 34,
  JOB_NETWORK: 40,
  JOB: 42,
  EXECUTABLE: 44,
  COMMAND_MODE: 46,
  SQL_FILE: 50,
  PLSQL_PACKAGE: 56,
  PROCEDURE: 58,
  FUNCTION: 58,
  TRIGGER: 60,
  VIEW: 70,
  MATERIALIZED_VIEW: 72,
  TABLE: 80,
  COLUMN: 82,
  EXTERNAL_SYSTEM: 90,
  EXTERNAL_API_OPERATION: 92,
  EXTERNAL_DATABASE_OBJECT: 94,
  UNRESOLVED_REFERENCE: 96,
};

export const modeLabels = {
  "1": "1 hop",
  W: "Write",
  R: "Read",
  E: "Explore",
};

export const nodeTypeKinds = {
  DATABASE: "table",
  TABLE: "table",
  COLUMN: "table",
  VIEW: "table",
  MATERIALIZED_VIEW: "table",
  SCREEN: "application",
  API_APPLICATION: "application",
  UI_ACTION: "code",
  API_OPERATION: "code",
  CONTROLLER: "code",
  SERVICE: "code",
  REPOSITORY: "repository",
  JOB_NETWORK: "sequence",
  JOB: "sequence",
  EXECUTABLE: "code",
  COMMAND_MODE: "code",
  PLSQL_PACKAGE: "file",
  PROCEDURE: "code",
  FUNCTION: "code",
  TRIGGER: "trigger",
  SQL_FILE: "file",
  EXTERNAL_SYSTEM: "unknown",
  EXTERNAL_API_OPERATION: "unknown",
  EXTERNAL_DATABASE_OBJECT: "table",
  UNRESOLVED_REFERENCE: "unknown",
};

export const directionLabels = {
  in: "Incoming",
  out: "Outgoing",
  both: "Both",
};

export const nodeIntent = {
  DATABASE: "Database boundary. Useful for finding owned tables, views, and unresolved external access.",
  TABLE: "Canonical business data object. Use Columns and Impact tabs to understand readers, writers, and CRUD flow.",
  COLUMN: "Authoritative table column. Use context and relations to assess field-level impact.",
  SCREEN: "User-facing entry point. Follow Flow to APIs, jobs, code, and data touched by this screen.",
  UI_ACTION: "User action within a screen. Follow Flow to see the operation started by the action.",
  API_APPLICATION: "API application boundary. Use Relations to inspect operations and downstream calls.",
  API_OPERATION: "API endpoint. Use Flow to verify downstream services, SQL, and table access.",
  JOB_NETWORK: "Batch job network. Use Relations to inspect contained jobs and execution order.",
  JOB: "Batch job. Use Overview for executable mapping and Flow for tables touched by the job.",
  EXECUTABLE: "Runtime executable. Use Flow to trace command modes, SQL files, procedures, and table access.",
  COMMAND_MODE: "Executable command mode. Use Relations to understand mode-specific behavior.",
  PLSQL_PACKAGE: "PL/SQL package container. Use Relations to inspect procedures/functions and dependencies.",
  PROCEDURE: "Executable database routine. Use Flow and Evidence to confirm calls and table operations.",
  FUNCTION: "Executable database function. Use Flow and Evidence to confirm calls and table operations.",
  TRIGGER: "Database trigger. Use Relations and Evidence to confirm firing table and side effects.",
  VIEW: "Derived database object. Use Relations to inspect source tables and dependent consumers.",
  MATERIALIZED_VIEW: "Persisted derived object. Use Relations and Impact to inspect dependencies and refresh risk.",
  SQL_FILE: "SQL script. Use Flow and Evidence to verify statements and target tables.",
  EXTERNAL_SYSTEM: "Boundary outside this code map. Treat outgoing/incoming edges as integration touchpoints.",
  EXTERNAL_API_OPERATION: "External API dependency. Use Evidence to confirm where it is called.",
  EXTERNAL_DATABASE_OBJECT: "Database object outside authoritative catalog. Review issues and evidence before relying on it.",
  UNRESOLVED_REFERENCE: "Reference not resolved to a canonical node. Review issues and evidence to decide mapping or cleanup.",
};

export const actorTypes = new Set([
  "SCREEN",
  "UI_ACTION",
  "API_OPERATION",
  "JOB",
  "EXECUTABLE",
  "COMMAND_MODE",
  "PROCEDURE",
  "FUNCTION",
  "TRIGGER",
  "SQL_FILE",
]);

export const importantPropertyKeys = [
  "method",
  "route",
  "path",
  "http_method",
  "database",
  "table_code",
  "column_code",
  "data_type",
  "nullable",
  "ordinal_position",
  "jobnet_id",
  "job_id",
  "job_system",
  "executable_scope",
  "executable_name",
  "arguments",
  "predecessor_job_id",
  "package_name",
  "routine_name",
  "source_path",
  "line",
];

export const operationDetails = {
  R: { label: "Read", title: "Reads", tone: "read", action: "reads data from", impactAction: "reads this data" },
  W: { label: "Write", title: "Writes", tone: "write", action: "changes data in", impactAction: "changes this data" },
};

export const actorTypeLabels = {
  SCREEN: "Screen",
  UI_ACTION: "UI action",
  API_OPERATION: "API",
  JOB: "Batch job",
  EXECUTABLE: "Executable",
  COMMAND_MODE: "Command mode",
  PROCEDURE: "Procedure",
  FUNCTION: "Function",
  TRIGGER: "Trigger",
  SQL_FILE: "SQL script",
};

export function edgeColor(type) {
  if (["READS", "REMOTE_READS"].includes(type)) return "#2563eb";
  if (["WRITES", "INSERTS", "UPDATES", "DELETES", "MERGES"].includes(type)) return "#16a34a";
  if (["CALLS", "CALLS_API", "STARTS"].includes(type)) return "#f97316";
  if (type === "TRIGGERS") return "#dc2626";
  if (type === "CONTAINS") return "#64748b";
  return "#8b5cf6";
}
