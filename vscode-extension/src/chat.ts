import * as vscode from "vscode";
import { GraphContext, Mode, RetrieveOptions } from "./core/retriever";
import { GraphService } from "./graphService";

const INSTRUCTIONS = `You are Code Map, a business analyst for this codebase.
Answer ONLY from the facts below: a slice of a knowledge graph extracted from the source code
(screens, APIs, methods, PL/SQL, SQL files, tables, batch jobs) joined with the business dictionary.
- Cite the facts you use inline with their ids, e.g. [N3], [E2], [C1], [F1], [I1].
- Start with the business answer in plain words: what happens for the business and which business data changes.
- Then give the technical trace (screen → API → code → table, job → program → table) with file:line where the facts have it.
- Then list gaps and risks: business terms with no linked code, unmapped jobs, extractor issues, writes you cannot see.
- Put business names (Vietnamese / Japanese / English from the facts) next to technical names.
- If the facts do not answer the question, say what is missing and what to ask or open next. Never invent tables, APIs, rules or values.
- Reply in the language of the user's question.`;

const MODE_INSTRUCTIONS: Record<Mode, string> = {
  ask: "",
  impact: "Task: impact analysis. Group everything that reads, writes, calls or depends on the item by operation, and say what breaks if it changes.",
  flow: "Task: describe the end-to-end flow step by step, from the entry point (screen, API, job) down to the data it reads and writes.",
  file: "Task: explain the business purpose of the code in the given file or selection, and where it sits in the business flows.",
  gaps: "Task: summarise the gaps as a prioritised list; for each, say how to close it (add a dictionary alias, map an executable, fix the extractor input).",
};

export function registerChat(context: vscode.ExtensionContext, graph: GraphService): void {
  const participant = vscode.chat.createChatParticipant("codemap.chat", (request, chatContext, stream, token) =>
    handle(graph, request, chatContext, stream, token),
  );
  participant.iconPath = new vscode.ThemeIcon("type-hierarchy");
  participant.followupProvider = {
    provideFollowups(result) {
      const metadata = result.metadata as { table?: string; entry?: string } | undefined;
      const followups: vscode.ChatFollowup[] = [];
      if (metadata?.table) followups.push({ prompt: metadata.table, command: "impact", label: `Impact of ${metadata.table}` });
      if (metadata?.entry) followups.push({ prompt: metadata.entry, command: "flow", label: `Flow of ${metadata.entry}` });
      followups.push({ prompt: "", command: "gaps", label: "Business gaps in the graph" });
      return followups;
    },
  };
  context.subscriptions.push(participant);
}

async function handle(
  graph: GraphService,
  request: vscode.ChatRequest,
  chatContext: vscode.ChatContext,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
): Promise<vscode.ChatResult> {
  const retriever = await graph.ready();
  if (!retriever) {
    stream.markdown("No Code Map graph is loaded yet. Run the extract, or point the extension at an existing `codemap-rag.sqlite`.\n\n");
    stream.button({ command: "codemap.runExtract", title: "Run Extract" });
    stream.button({ command: "codemap.selectDatabase", title: "Select Graph Database…" });
    return {};
  }
  const mode: Mode = request.command === "explain" ? "file" : ((request.command as Mode | undefined) ?? "ask");
  const options = retrievalOptions(mode);
  let question = request.prompt.trim();
  if (mode === "file") {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      stream.markdown("Open a source file first; `/explain` explains the active file or selection.");
      return {};
    }
    options.file = graph.toGraphPath(editor.document.uri.fsPath);
    if (!editor.selection.isEmpty) options.lines = [editor.selection.start.line + 1, editor.selection.end.line + 1];
    question ||= `Explain the business role of ${options.file}`;
  } else if (!question && mode !== "gaps") {
    stream.markdown("Ask a question, e.g. `@codemap hủy đơn hàng ghi vào bảng nào?` or `@codemap /impact ORDER_HEADER`.");
    return {};
  }

  stream.progress("Searching the Code Map graph…");
  const context = retriever.retrieve(question, options);
  if (mode !== "gaps" && !context.nodes.length) {
    stream.markdown("The graph has nothing matching this question. Try a table, API, screen or job name, or a term from the business dictionary.");
    return {};
  }
  stream.progress(`Found ${context.nodes.length} nodes, ${context.edges.length} relations, ${context.concepts.length} business terms.`);

  const messages = [
    vscode.LanguageModelChatMessage.User(`${INSTRUCTIONS}\n${MODE_INSTRUCTIONS[mode]}\n\n# Facts\n${context.text}`),
    ...history(chatContext),
    vscode.LanguageModelChatMessage.User(question || "List the business gaps."),
  ];
  try {
    const response = await request.model.sendRequest(messages, {}, token);
    for await (const fragment of response.text) stream.markdown(fragment);
  } catch (error) {
    if (error instanceof vscode.LanguageModelError) {
      stream.markdown(`\n\nThe language model refused or failed (${error.code}): ${error.message}\n\nThe facts the graph found:\n\n`);
      stream.markdown("```text\n" + context.text + "\n```");
      return { errorDetails: { message: error.message } };
    }
    throw error;
  }
  addReferences(graph, context, stream);
  return { metadata: followupMetadata(context) };
}

export function retrievalOptions(mode: Mode): RetrieveOptions {
  const config = vscode.workspace.getConfiguration("codemap.retrieval");
  const hops = config.get<number>("hops", 2);
  return {
    mode,
    maxNodes: config.get<number>("maxNodes", 40),
    hops: mode === "flow" ? Math.max(hops, 4) : mode === "impact" ? Math.max(hops, 3) : hops,
    maxContextChars: config.get<number>("maxContextChars", 16000),
  };
}

function history(chatContext: vscode.ChatContext): vscode.LanguageModelChatMessage[] {
  const messages: vscode.LanguageModelChatMessage[] = [];
  for (const turn of chatContext.history.slice(-6)) {
    if (turn instanceof vscode.ChatRequestTurn && turn.participant === "codemap.chat") {
      messages.push(vscode.LanguageModelChatMessage.User(turn.prompt));
    } else if (turn instanceof vscode.ChatResponseTurn && turn.participant === "codemap.chat") {
      const text = turn.response
        .map((part) => (part instanceof vscode.ChatResponseMarkdownPart ? part.value.value : ""))
        .join("");
      if (text) messages.push(vscode.LanguageModelChatMessage.Assistant(text.slice(0, 4000)));
    }
  }
  return messages;
}

function addReferences(graph: GraphService, context: GraphContext, stream: vscode.ChatResponseStream): void {
  const seen = new Set<string>();
  for (const reference of context.references) {
    if (seen.size >= 15) break;
    const key = `${reference.file}:${reference.line ?? 0}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const uri = graph.resolveSource(reference.file);
    if (!uri) continue;
    const line = Math.max(0, (reference.line ?? 1) - 1);
    stream.reference(new vscode.Location(uri, new vscode.Position(line, 0)));
  }
}

function followupMetadata(context: GraphContext): { table?: string; entry?: string } {
  const table = context.seeds.find((node) => node.type === "TABLE") ?? context.nodes.find((node) => node.type === "TABLE");
  const entry = context.seeds.find((node) => ["API_OPERATION", "SCREEN", "JOB"].includes(node.type));
  return { table: table?.name, entry: entry?.name };
}
