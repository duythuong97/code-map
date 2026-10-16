// Graph RAG retrieval over codemap-rag.sqlite: find seed nodes for a question
// (business dictionary + name index), walk the graph a few hops, and render the
// facts as numbered lines the chat model must cite.
import { GraphDb, Param, Row, placeholders } from "./db";
import { compact, tokens } from "./text";

export type Mode = "ask" | "impact" | "flow" | "file" | "gaps";

export interface RetrieveOptions {
  mode?: Mode;
  maxNodes?: number;
  hops?: number;
  maxContextChars?: number;
  /** For mode "file": the file path relative to the source root (forward slashes). */
  file?: string;
  /** For mode "file": 1-based inclusive line range of the selection. */
  lines?: [number, number];
}

export interface GraphNode {
  id: number;
  stableId: string;
  type: string;
  name: string;
  qualifiedName: string;
  file: string | null;
  line: number | null;
  names: string;
  doc: string;
}

export interface GraphEdge {
  id: number;
  src: number;
  dst: number;
  type: string;
  operation: string;
  file: string | null;
  line: number | null;
  snippet: string | null;
}

export interface Concept {
  semanticId: string;
  labels: string[];
  definitions: string[];
  aliases: string[];
  linked: number[];
}

export interface Reference {
  label: string;
  file: string;
  line: number | null;
}

/** A node as the map draws it. */
export interface MapNode {
  id: number;
  type: string;
  name: string;
  label: string;
  names: string;
  file: string | null;
  line: number | null;
  layer: number;
}

export interface MapEdge {
  id: number;
  src: number;
  dst: number;
  type: string;
  operation: string;
  category: "write" | "read" | "call" | "business" | "structure" | "other";
  file: string | null;
  line: number | null;
}

export interface MapGraph {
  nodes: MapNode[];
  edges: MapEdge[];
  focus: number[];
  truncated: boolean;
}

export interface GraphContext {
  text: string;
  seeds: GraphNode[];
  nodes: GraphNode[];
  edges: GraphEdge[];
  concepts: Concept[];
  references: Reference[];
  truncated: boolean;
}

const EDGE_PRIORITY: Record<string, number> = {
  WRITES_TO: 10, INSERTS: 10, UPDATES: 10, DELETES: 10, MERGES: 10, WRITES: 10, LOADS_INTO: 10,
  READS_FROM: 9, READS: 9, REMOTE_READS: 9,
  HANDLES_API: 8, HANDLED_BY: 8, CALLS_API: 8, STARTS: 8, INVOKES: 8, TRIGGERS: 8,
  DEPENDS_ON: 7, CALLS: 7, USES_SEQUENCE: 6, RESOLVES_TO: 6, MAPS_TO: 5, ENTRY_IN: 5, IMPLEMENTS: 5,
  BELONGS_TO: 3, USES: 2, CONTAINS: 2, PROJECT_REFERENCE: 1,
};
const TYPE_PRIOR: Record<string, number> = {
  TABLE: 1.4, API_OPERATION: 1.4, PROCEDURE: 1.3, FUNCTION: 1.2, PLSQL_PACKAGE: 1.2, SCREEN: 1.3, UI_ACTION: 1.2,
  JOB: 1.3, JOB_NETWORK: 1.2, COMMAND_MODE: 1.2, TRIGGER: 1.2, VIEW: 1.3, SQL_FILE: 1.1, METHOD: 1.0,
  COLUMN: 0.7, CSHARP_TYPE: 0.8, FILE: 0.4, INLINE_SQL: 0.4, UNRESOLVED_REFERENCE: 0.3, SYSTEM: 0.3, REPOSITORY: 0.3,
  DATABASE: 0.3, DATABASE_SCHEMA: 0.3, DOTNET_PROJECT: 0.3, DOTNET_SOLUTION: 0.2,
};
const MAX_SEEDS = 10;
const EDGE_SCAN_LIMIT = 500;

export class Retriever {
  private nodeCount: number;
  private labelTotals = new Map<number, number>();
  readonly meta: Record<string, string> = {};

  constructor(private readonly db: GraphDb) {
    for (const row of db.all("SELECT key, value FROM meta")) this.meta[String(row.key)] = String(row.value);
    this.nodeCount = Number(db.all("SELECT count(*) AS n FROM nodes")[0]?.n ?? 0);
    for (const row of db.all("SELECT concept_id, count(*) AS n FROM concept_terms WHERE label_terms=1 GROUP BY concept_id")) {
      this.labelTotals.set(Number(row.concept_id), Number(row.n));
    }
  }

  stats(): Record<string, number> {
    const stats: Record<string, number> = {};
    for (const table of ["nodes", "edges", "concepts", "concept_links", "flows", "issues"]) {
      stats[table] = Number(this.db.all(`SELECT count(*) AS n FROM ${table}`)[0]?.n ?? 0);
    }
    return stats;
  }

  retrieve(question: string, options: RetrieveOptions = {}): GraphContext {
    const mode = options.mode ?? "ask";
    const maxNodes = options.maxNodes ?? 40;
    const hops = options.hops ?? (mode === "flow" ? 4 : mode === "impact" ? 3 : 2);
    if (mode === "gaps") return this.gaps(options.maxContextChars ?? 16000);

    const concepts = mode === "file" ? [] : this.matchConcepts(question);
    const scores =
      mode === "file" && options.file ? this.fileSeeds(options.file, options.lines) : this.termSeeds(question);
    // Code linked to a matched business term ranks with the best direct name match.
    const bonus = Math.max(1, 0.8 * Math.max(0, ...scores.values()));
    for (const concept of concepts) {
      for (const id of concept.linked) scores.set(id, (scores.get(id) ?? 0) + bonus);
    }
    const ranked = [...scores.entries()].sort((a, b) => b[1] - a[1]);
    const cutoff = (ranked[0]?.[1] ?? 0) * 0.3;
    const seedIds = ranked.filter(([, score]) => score >= cutoff).slice(0, MAX_SEEDS).map(([id]) => id);

    const direction = mode === "impact" ? "in" : mode === "flow" ? "out" : "both";
    const { nodeIds, edges } = this.expand(seedIds, hops, maxNodes, direction);
    const nodes = this.nodes([...nodeIds]);
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const seeds = seedIds.map((id) => byId.get(id)).filter((node): node is GraphNode => !!node);
    const allConcepts = mergeConcepts(concepts, this.conceptsForNodes(seedIds));
    return this.render(seeds, nodes, edges, allConcepts, options.maxContextChars ?? 16000);
  }

  /** Business dictionary hits: label phrases ("hủy đơn hàng") and code aliases ("cancelOrder"). */
  matchConcepts(question: string): Concept[] {
    const terms = queryTerms(question);
    if (!terms.length) return [];
    const rows = this.db.all(
      `SELECT ct.concept_id, ct.term, ct.label_terms, c.semantic_id FROM concept_terms ct JOIN concepts c ON c.id=ct.concept_id
       WHERE ct.term IN (${placeholders(terms.length)}) AND c.status <> 'deprecated'`,
      terms,
    );
    const hits = new Map<number, { semanticId: string; label: Set<string>; alias: Set<string> }>();
    for (const row of rows) {
      const id = Number(row.concept_id);
      const hit = hits.get(id) ?? { semanticId: String(row.semantic_id), label: new Set(), alias: new Set() };
      (Number(row.label_terms) ? hit.label : hit.alias).add(String(row.term));
      hits.set(id, hit);
    }
    // "đơn hàng" (Order) is dropped when "hủy đơn hàng" (Cancel order) matched the same words and more.
    for (const [id, hit] of hits) {
      const dominated = [...hits.entries()].some(
        ([other, candidate]) =>
          other !== id &&
          candidate.label.size > hit.label.size &&
          [...hit.label].every((term) => candidate.label.has(term)) &&
          [...hit.alias].every((term) => spelledBy(term, hit.label)),
      );
      if (dominated) hits.delete(id);
    }
    const bySemantic = new Map<string, number>();
    for (const [id, hit] of hits) {
      const total = this.labelTotals.get(id) ?? 0;
      let score = 0;
      if (total && hit.label.size === total) score = 2 * total;
      else if (total && hit.label.size / total >= 0.66) score = hit.label.size;
      // Alias terms are whole aliases ("cancelorder"), a strong signal.
      score += 3 * [...hit.alias].filter((term) => !hit.label.has(term)).length;
      if (score > (bySemantic.get(hit.semanticId) ?? 0)) bySemantic.set(hit.semanticId, score);
    }
    const best = Math.max(0, ...bySemantic.values());
    const chosen = [...bySemantic.entries()]
      .filter(([, score]) => score > 0 && score >= best * 0.75)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3)
      .map(([semanticId]) => semanticId);
    return chosen.map((semanticId) => this.concept(semanticId));
  }

  private concept(semanticId: string): Concept {
    const rows = this.db.all("SELECT label, definition, aliases FROM concepts WHERE semantic_id=? ORDER BY locale", [semanticId]);
    const aliases = new Set<string>();
    for (const row of rows) for (const alias of JSON.parse(String(row.aliases || "[]")) as string[]) aliases.add(alias);
    return {
      semanticId,
      labels: rows.map((row) => String(row.label)),
      definitions: rows.map((row) => String(row.definition)),
      aliases: [...aliases],
      linked: this.db.all("SELECT node_id FROM concept_links WHERE semantic_id=?", [semanticId]).map((row) => Number(row.node_id)),
    };
  }

  private conceptsForNodes(ids: number[]): Concept[] {
    if (!ids.length) return [];
    const semanticIds = new Set(
      this.db
        .all(`SELECT DISTINCT semantic_id FROM concept_links WHERE node_id IN (${placeholders(ids.length)})`, ids)
        .map((row) => String(row.semantic_id)),
    );
    return [...semanticIds].map((id) => this.concept(id));
  }

  /** Name index hits weighted by rarity (idf) and node type. */
  private termSeeds(question: string): Map<number, number> {
    const terms = queryTerms(question);
    const result = new Map<number, number>();
    if (!terms.length) return result;
    const frequency = new Map<string, number>();
    for (const row of this.db.all(
      `SELECT term, count(*) AS n FROM terms WHERE term IN (${placeholders(terms.length)}) GROUP BY term`,
      terms,
    )) {
      frequency.set(String(row.term), Number(row.n));
    }
    const common = Math.max(50, this.nodeCount * 0.15);
    let kept = [...frequency.keys()].filter((term) => (frequency.get(term) ?? 0) <= common);
    if (!kept.length) kept = [...frequency.keys()].sort((a, b) => (frequency.get(a) ?? 0) - (frequency.get(b) ?? 0)).slice(0, 1);
    if (!kept.length) return result;
    const matched = new Map<number, number>();
    for (const row of this.db.all(
      `SELECT t.node_id, t.term, t.weight, n.type FROM terms t JOIN nodes n ON n.id=t.node_id
       WHERE t.term IN (${placeholders(kept.length)})`,
      kept,
    )) {
      const id = Number(row.node_id);
      const idf = Math.log(1 + this.nodeCount / (frequency.get(String(row.term)) ?? 1));
      const prior = TYPE_PRIOR[String(row.type)] ?? 1;
      result.set(id, (result.get(id) ?? 0) + Number(row.weight) * idf * prior);
      matched.set(id, (matched.get(id) ?? 0) + 1);
    }
    for (const [id, count] of matched) result.set(id, (result.get(id) ?? 0) * (1 + 0.5 * (count - 1)));
    return result;
  }

  private fileSeeds(file: string, lines?: [number, number]): Map<number, number> {
    const normalized = file.replace(/\\/g, "/").replace(/^\.?\//, "");
    let rows = this.db.all("SELECT id, type, line FROM nodes WHERE file=?", [normalized]);
    if (!rows.length) {
      const tail = normalized.split("/").slice(-3).join("/");
      rows = this.db.all("SELECT id, type, line FROM nodes WHERE file LIKE ?", [`%${tail}`]);
    }
    const result = new Map<number, number>();
    for (const row of rows) {
      const line = row.line == null ? null : Number(row.line);
      const inside = !lines || (line != null && line >= lines[0] && line <= lines[1]);
      const score = (TYPE_PRIOR[String(row.type)] ?? 1) * (inside ? 10 : 1);
      result.set(Number(row.id), score);
    }
    return result;
  }

  /** Seed nodes for a free-text query (business terms and names), best first. */
  findNodes(query: string, limit = 20): GraphNode[] {
    const scores = this.termSeeds(query);
    const bonus = Math.max(1, 0.8 * Math.max(0, ...scores.values()));
    for (const concept of this.matchConcepts(query)) {
      for (const id of concept.linked) scores.set(id, (scores.get(id) ?? 0) + bonus);
    }
    const ids = [...scores.entries()].sort((a, b) => b[1] - a[1]).slice(0, limit).map(([id]) => id);
    const byId = new Map(this.nodes(ids).map((node) => [node.id, node]));
    return ids.map((id) => byId.get(id)).filter((node): node is GraphNode => !!node);
  }

  /** The map around some nodes: a bounded walk in the given direction plus the edges between what it reached. */
  mapGraph(
    seedIds: number[],
    options: { hops?: number; direction?: "in" | "out" | "both"; maxNodes?: number } = {},
  ): MapGraph {
    const maxNodes = options.maxNodes ?? 60;
    const { nodeIds, edges } = this.expand(seedIds, options.hops ?? 1, maxNodes, options.direction ?? "both");
    return this.mapOf([...nodeIds], edges, seedIds, nodeIds.size >= maxNodes);
  }

  /** The map of exactly these nodes (e.g. the facts an answer cited) and the edges among them. */
  mapAmong(ids: number[], focus: number[] = []): MapGraph {
    const unique = [...new Set(ids)].slice(0, 300);
    if (!unique.length) return { nodes: [], edges: [], focus: [], truncated: false };
    const marks = placeholders(unique.length);
    const edges = this.db
      .all(
        `SELECT id, src, dst, type, operation, file, line, snippet FROM edges WHERE src IN (${marks}) AND dst IN (${marks}) LIMIT 2000`,
        [...unique, ...unique],
      )
      .map(toEdge);
    return this.mapOf(unique, edges, focus, false);
  }

  private mapOf(ids: number[], edges: GraphEdge[], focus: number[], truncated: boolean): MapGraph {
    const nodes = this.nodes(ids).map((node) => ({
      id: node.id,
      type: node.type,
      name: node.name,
      label: displayName(node),
      names: node.names,
      file: node.file,
      line: node.line,
      layer: LAYER[node.type] ?? 2,
    }));
    const present = new Set(nodes.map((node) => node.id));
    return {
      nodes,
      edges: edges
        .filter((edge) => present.has(edge.src) && present.has(edge.dst))
        .map((edge) => ({
          id: edge.id,
          src: edge.src,
          dst: edge.dst,
          type: edge.type,
          operation: edge.operation,
          category: edgeCategory(edge.type),
          file: edge.file,
          line: edge.line,
        })),
      focus: focus.filter((id) => present.has(id)),
      truncated,
    };
  }

  private expand(seeds: number[], hops: number, maxNodes: number, direction: "in" | "out" | "both") {
    const nodeIds = new Set<number>(seeds);
    const edges = new Map<number, GraphEdge>();
    let frontier = [...seeds];
    for (let hop = 1; hop <= hops && frontier.length && nodeIds.size < maxNodes; hop++) {
      const perNode = hop === 1 ? 12 : 6;
      const next: number[] = [];
      for (const id of frontier) {
        if (nodeIds.size >= maxNodes) break;
        const candidates: GraphEdge[] = [];
        if (direction !== "in") candidates.push(...this.edgeRows("src", id));
        if (direction !== "out") candidates.push(...this.edgeRows("dst", id));
        candidates.sort((a, b) => (EDGE_PRIORITY[b.type] ?? 3) - (EDGE_PRIORITY[a.type] ?? 3));
        let taken = 0;
        for (const edge of candidates) {
          if (taken >= perNode || nodeIds.size >= maxNodes) break;
          if ((EDGE_PRIORITY[edge.type] ?? 3) <= 2 && hop > 1) continue;
          const other = edge.src === id ? edge.dst : edge.src;
          edges.set(edge.id, edge);
          taken++;
          if (!nodeIds.has(other)) {
            nodeIds.add(other);
            next.push(other);
          }
        }
      }
      frontier = next;
    }
    return { nodeIds, edges: [...edges.values()] };
  }

  private edgeRows(column: "src" | "dst", id: number): GraphEdge[] {
    return this.db
      .all(`SELECT id, src, dst, type, operation, file, line, snippet FROM edges WHERE ${column}=? LIMIT ${EDGE_SCAN_LIMIT}`, [id])
      .map(toEdge);
  }

  nodes(ids: number[]): GraphNode[] {
    if (!ids.length) return [];
    return this.db
      .all(
        `SELECT id, stable_id, type, name, qualified_name, file, line, names, doc FROM nodes WHERE id IN (${placeholders(ids.length)})`,
        ids,
      )
      .map(toNode);
  }

  private flows(ids: number[]): Row[] {
    if (!ids.length) return [];
    const marks = placeholders(ids.length);
    return this.db.all(
      `SELECT id, type, start_id, target_id, operation, path, complete FROM flows
       WHERE start_id IN (${marks}) OR target_id IN (${marks}) ORDER BY complete DESC, id LIMIT 12`,
      [...ids, ...ids],
    );
  }

  private issues(ids: number[]): Row[] {
    if (!ids.length) return [];
    return this.db.all(
      `SELECT node_id, type, severity, message, file, line FROM issues WHERE node_id IN (${placeholders(ids.length)}) LIMIT 20`,
      ids,
    );
  }

  private render(seeds: GraphNode[], nodes: GraphNode[], edges: GraphEdge[], concepts: Concept[], budget: number): GraphContext {
    const out = new Budget(budget);
    const label = new Map<number, string>();
    const ordered = [...seeds, ...nodes.filter((node) => !seeds.some((seed) => seed.id === node.id))];
    ordered.forEach((node, index) => label.set(node.id, `N${index + 1}`));
    const references: Reference[] = [];

    if (concepts.length) {
      out.add("## Business concepts (from the business dictionary)");
      concepts.forEach((concept, index) => {
        const linked = concept.linked.map((id) => label.get(id)).filter(Boolean).join(", ");
        out.add(
          `[C${index + 1}] ${concept.labels.join(" / ")} — ${concept.definitions.join(" / ")}` +
            (concept.aliases.length ? ` | code aliases: ${concept.aliases.join(", ")}` : "") +
            (linked ? ` | implemented by: ${linked}` : " | implemented by: (no linked code found)"),
        );
      });
    }
    out.add("## Code and data nodes" + (seeds.length ? " (first entries matched the question)" : ""));
    for (const node of ordered) {
      const where = node.file ? ` @ ${node.file}${node.line ? `:${node.line}` : ""}` : "";
      const names = node.names ? ` | business names: ${node.names}` : "";
      const doc = node.doc ? ` | comments: ${node.doc.slice(0, 300)}` : "";
      if (out.add(`[${label.get(node.id)}] ${node.type} ${displayName(node)}${names}${where}${doc}`) && node.file) {
        references.push({ label: `${label.get(node.id)} ${node.name}`, file: node.file, line: node.line });
      }
    }
    if (edges.length) {
      out.add("## Relations");
      edges
        .slice()
        .sort((a, b) => (EDGE_PRIORITY[b.type] ?? 3) - (EDGE_PRIORITY[a.type] ?? 3))
        .forEach((edge, index) => {
          const op = edge.operation && edge.operation !== edge.type ? ` (${edge.operation})` : "";
          const where = edge.file ? ` @ ${edge.file}${edge.line ? `:${edge.line}` : ""}` : "";
          const snippet = edge.snippet ? ` \`${edge.snippet.replace(/\s+/g, " ").slice(0, 160)}\`` : "";
          if (out.add(`[E${index + 1}] ${label.get(edge.src)} -${edge.type}${op}-> ${label.get(edge.dst)}${where}${snippet}`) && edge.file) {
            references.push({ label: `E${index + 1} ${edge.type}`, file: edge.file, line: edge.line });
          }
        });
    }
    const ids = ordered.map((node) => node.id);
    const flows = this.flows(seeds.map((node) => node.id).concat(ids.slice(0, 20)));
    if (flows.length) {
      const pathIds = new Set<number>();
      for (const flow of flows) for (const id of JSON.parse(String(flow.path)) as number[]) pathIds.add(id);
      const names = new Map(this.nodes([...pathIds]).map((node) => [node.id, node]));
      out.add("## End-to-end data flows (precomputed by the extractor)");
      flows.forEach((flow, index) => {
        const chain = (JSON.parse(String(flow.path)) as number[])
          .map((id) => {
            const node = names.get(id);
            return node ? `${label.get(id) ? label.get(id) + " " : ""}${node.type} ${node.name}` : `#${id}`;
          })
          .join(" → ");
        out.add(`[F${index + 1}] ${flow.type}${flow.operation ? ` ${flow.operation}` : ""}${Number(flow.complete) ? "" : " (incomplete)"}: ${chain}`);
      });
    }
    const issues = this.issues(ids);
    if (issues.length) {
      out.add("## Known gaps / issues reported by the extractor");
      issues.forEach((issue, index) => {
        const where = issue.file ? ` @ ${issue.file}${issue.line ? `:${issue.line}` : ""}` : "";
        out.add(`[I${index + 1}] ${issue.severity} ${issue.type} on ${label.get(Number(issue.node_id)) ?? "?"}: ${issue.message}${where}`);
      });
    }
    return { text: out.text(), seeds, nodes: ordered, edges, concepts, references, truncated: out.truncated };
  }

  /** Business terms without code, jobs without executables, and other extractor issues. */
  private gaps(budget: number): GraphContext {
    const out = new Budget(budget);
    const unlinked = this.db.all(
      `SELECT c.semantic_id, group_concat(c.label, ' / ') AS labels, max(c.aliases) AS aliases FROM concepts c
       WHERE c.status <> 'deprecated' AND NOT EXISTS (SELECT 1 FROM concept_links l WHERE l.semantic_id=c.semantic_id)
       GROUP BY c.semantic_id`,
    );
    out.add("## Business concepts with no linked code");
    if (!unlinked.length) out.add("(none)");
    unlinked.forEach((row, index) => out.add(`[C${index + 1}] ${row.labels} (${row.semantic_id}) aliases: ${row.aliases}`));
    out.add("## Extractor issues by type");
    for (const row of this.db.all("SELECT type, severity, count(*) AS n FROM issues GROUP BY type, severity ORDER BY n DESC")) {
      out.add(`- ${row.severity} ${row.type}: ${row.n}`);
    }
    out.add("## Issue samples");
    const references: Reference[] = [];
    this.db
      .all(
        `SELECT i.type, i.severity, i.message, i.file, i.line, n.type AS node_type, n.name FROM issues i
         LEFT JOIN nodes n ON n.id=i.node_id ORDER BY CASE i.severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, i.type LIMIT 60`,
      )
      .forEach((row, index) => {
        const where = row.file ? ` @ ${row.file}${row.line ? `:${row.line}` : ""}` : "";
        if (out.add(`[I${index + 1}] ${row.severity} ${row.type}${row.name ? ` on ${row.node_type} ${row.name}` : ""}: ${row.message}${where}`) && row.file) {
          references.push({ label: `I${index + 1}`, file: String(row.file), line: row.line == null ? null : Number(row.line) });
        }
      });
    return { text: out.text(), seeds: [], nodes: [], edges: [], concepts: [], references, truncated: out.truncated };
  }
}

/**
 * Query tokens (plus English singulars) and compact forms of 1-3 word windows, so
 * "ORDER_HEADER", "cancel order" and "hủy đơn" match whole names and aliases.
 */
export function queryTerms(question: string): string[] {
  const terms = new Set<string>();
  for (const token of tokens(question)) {
    terms.add(token);
    if (/^[a-z]{3,}s$/.test(token)) terms.add(token.slice(0, -1));
  }
  const words = question.split(/[\s,;:!?()"'`\[\]{}]+/).filter(Boolean);
  for (let size = 1; size <= 3; size++) {
    for (let i = 0; i + size <= words.length; i++) {
      const value = compact(words.slice(i, i + size).join(""));
      if (value.length >= 3) terms.add(value);
    }
  }
  return [...terms].slice(0, 80);
}

// Left-to-right layers of the map: entry points, application code, database code, data.
const LAYER: Record<string, number> = {
  JOB_NETWORK: 0, JOB: 0, SCREEN: 0, UI_ACTION: 0, ANGULAR_COMPONENT: 0, ANGULAR_PROJECT: 0,
  EXECUTABLE: 1, EXECUTABLE_ENTRY_POINT: 1, COMMAND_MODE: 1, API_OPERATION: 1, API_CALL_REFERENCE: 1, ANGULAR_SERVICE: 1,
  API_APPLICATION: 1, CONTROLLER: 2, SERVICE: 2, REPOSITORY: 2, METHOD: 2, CSHARP_TYPE: 2, FILE: 2,
  PLSQL_PACKAGE: 3, PROCEDURE: 3, FUNCTION: 3, LOCAL_ROUTINE: 3, TRIGGER: 3, SQL_FILE: 3, INLINE_SQL: 3, LOADER_CONTROL: 3,
  TABLE: 4, VIEW: 4, COLUMN: 4, SEQUENCE: 4, EXTERNAL_DATABASE_OBJECT: 4, DATABASE_LINK: 4, DATABASE_SCHEMA: 4, DATABASE: 4,
};

export function edgeCategory(type: string): MapEdge["category"] {
  if (["WRITES_TO", "INSERTS", "UPDATES", "DELETES", "MERGES", "WRITES", "LOADS_INTO"].includes(type)) return "write";
  if (["READS_FROM", "READS", "REMOTE_READS"].includes(type)) return "read";
  if (["CALLS", "CALLS_API", "HANDLES_API", "HANDLED_BY", "TRIGGERS", "RESOLVES_TO", "ENTRY_IN"].includes(type)) return "call";
  if (["STARTS", "INVOKES", "DEPENDS_ON"].includes(type)) return "business";
  if (["CONTAINS", "BELONGS_TO", "PROJECT_REFERENCE"].includes(type)) return "structure";
  return "other";
}

/** True when an alias hit is just the label words glued together ("donhang" from "don", "hang"). */
function spelledBy(term: string, words: Set<string>): boolean {
  let rest = term;
  for (const word of [...words].sort((a, b) => b.length - a.length)) rest = rest.split(word).join("");
  return rest === "";
}

function displayName(node: GraphNode): string {
  if (["METHOD", "PROCEDURE", "FUNCTION", "LOCAL_ROUTINE"].includes(node.type)) {
    const parts = node.qualifiedName
      .replace(/\(.*$/, "")
      .split(/[./\\:]/)
      .filter((part) => part && !/^\d+$/.test(part) && !/^(cs|ts|js|sql|pkb|pks|pls)$/i.test(part));
    const owner = parts.length >= 2 ? parts[parts.length - 2] : "";
    if (owner && owner !== node.name) return `${owner}.${node.name}`;
  }
  return node.name;
}

function mergeConcepts(first: Concept[], second: Concept[]): Concept[] {
  const seen = new Set(first.map((concept) => concept.semanticId));
  return [...first, ...second.filter((concept) => !seen.has(concept.semanticId))].slice(0, 8);
}

function toNode(row: Row): GraphNode {
  return {
    id: Number(row.id),
    stableId: String(row.stable_id),
    type: String(row.type),
    name: String(row.name),
    qualifiedName: String(row.qualified_name ?? ""),
    file: row.file == null ? null : String(row.file),
    line: row.line == null ? null : Number(row.line),
    names: String(row.names ?? ""),
    doc: String(row.doc ?? ""),
  };
}

function toEdge(row: Row): GraphEdge {
  return {
    id: Number(row.id),
    src: Number(row.src),
    dst: Number(row.dst),
    type: String(row.type),
    operation: String(row.operation ?? ""),
    file: row.file == null ? null : String(row.file),
    line: row.line == null ? null : Number(row.line),
    snippet: row.snippet == null ? null : String(row.snippet),
  };
}

class Budget {
  private lines: string[] = [];
  private used = 0;
  truncated = false;
  constructor(private readonly limit: number) {}
  add(line: string): boolean {
    if (this.used + line.length + 1 > this.limit) {
      this.truncated = true;
      return false;
    }
    this.lines.push(line);
    this.used += line.length + 1;
    return true;
  }
  text(): string {
    return this.lines.join("\n") + (this.truncated ? "\n(… more facts omitted: context budget reached)" : "");
  }
}

export type { Param };
