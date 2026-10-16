import * as path from "node:path";
import * as vscode from "vscode";
import { Mode } from "./core/retriever";
import { retrievalOptions } from "./chat";
import { GraphService } from "./graphService";

interface QueryInput {
  question: string;
  mode?: "ask" | "impact" | "flow";
}

interface FileInput {
  path: string;
}

/** Tools for agent mode: the model calls them itself when it needs graph facts. */
export function registerTools(context: vscode.ExtensionContext, graph: GraphService): void {
  context.subscriptions.push(
    vscode.lm.registerTool<QueryInput>("codemap_query", {
      prepareInvocation(options) {
        return { invocationMessage: `Searching the Code Map graph for "${options.input.question}"` };
      },
      async invoke(options) {
        const mode: Mode = options.input.mode ?? "ask";
        return text(await query(graph, options.input.question, mode));
      },
    }),
    vscode.lm.registerTool<FileInput>("codemap_file_context", {
      prepareInvocation(options) {
        return { invocationMessage: `Reading Code Map facts for ${path.basename(options.input.path)}` };
      },
      async invoke(options) {
        const retriever = await graph.ready();
        if (!retriever) return text(NO_GRAPH);
        const absolute = path.isAbsolute(options.input.path)
          ? options.input.path
          : path.join(vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? "", options.input.path);
        const context = retriever.retrieve("", { ...retrievalOptions("file"), file: graph.toGraphPath(absolute) });
        return text(context.nodes.length ? context.text : `The graph has no nodes declared in ${options.input.path}.`);
      },
    }),
  );
}

const NO_GRAPH = "No Code Map graph is loaded (codemap-rag.sqlite not found). Ask the user to run 'Code Map: Run Extract'.";

async function query(graph: GraphService, question: string, mode: Mode): Promise<string> {
  const retriever = await graph.ready();
  if (!retriever) return NO_GRAPH;
  const context = retriever.retrieve(question, retrievalOptions(mode));
  return context.nodes.length || context.concepts.length
    ? `Facts from the Code Map graph (cite ids like [N1]):\n${context.text}`
    : "The Code Map graph has nothing matching this question.";
}

function text(value: string): vscode.LanguageModelToolResult {
  return new vscode.LanguageModelToolResult([new vscode.LanguageModelTextPart(value)]);
}
