import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import { registerChat } from "./chat";
import { MapPanel } from "./mapPanel";
import { DATABASE_NAME, expandWorkspace, GraphService } from "./graphService";
import { registerTools } from "./tools";

export function activate(context: vscode.ExtensionContext): void {
  const log = vscode.window.createOutputChannel("Code Map");
  const graph = new GraphService(context, log);
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 10);
  status.command = "codemap.status";
  context.subscriptions.push(log, graph, status);

  const refreshStatus = async () => {
    const retriever = await graph.ready();
    if (retriever) {
      const stats = retriever.stats();
      status.text = `$(type-hierarchy) Code Map ${stats.nodes.toLocaleString()}`;
      status.tooltip = `${graph.databasePath}\n${stats.nodes} nodes · ${stats.edges} relations · ${stats.concepts} business terms`;
    } else {
      status.text = "$(type-hierarchy) Code Map: no graph";
      status.tooltip = "Run 'Code Map: Run Extract' or select a codemap-rag.sqlite";
    }
    status.show();
  };
  context.subscriptions.push(graph.onDidChange(() => void refreshStatus()));

  registerChat(context, graph);
  registerTools(context, graph);

  context.subscriptions.push(
    vscode.commands.registerCommand("codemap.reload", async () => {
      await graph.reload();
      await refreshStatus();
    }),
    vscode.commands.registerCommand("codemap.selectDatabase", async () => {
      const picked = await vscode.window.showOpenDialog({
        canSelectMany: false,
        filters: { "Code Map graph": ["sqlite"] },
        openLabel: "Use this graph",
      });
      if (!picked?.length) return;
      const target = vscode.workspace.workspaceFolders?.length
        ? vscode.ConfigurationTarget.Workspace
        : vscode.ConfigurationTarget.Global;
      await vscode.workspace.getConfiguration("codemap").update("databasePath", picked[0].fsPath, target);
      await graph.reload();
      await refreshStatus();
    }),
    vscode.commands.registerCommand("codemap.status", async () => {
      const retriever = await graph.ready();
      if (!retriever) {
        const choice = await vscode.window.showWarningMessage(`No ${DATABASE_NAME} loaded.`, "Run Extract", "Select…");
        if (choice === "Run Extract") await vscode.commands.executeCommand("codemap.runExtract");
        if (choice === "Select…") await vscode.commands.executeCommand("codemap.selectDatabase");
        return;
      }
      const stats = retriever.stats();
      log.appendLine(`Status: ${graph.databasePath} (${graph.engine}) ${JSON.stringify(stats)} generated ${retriever.meta.generated_at}`);
      log.show(true);
    }),
    vscode.commands.registerCommand("codemap.runExtract", () => runExtract(graph, refreshStatus)),
    vscode.commands.registerCommand("codemap.showMap", (query?: string) => showMap(context, graph, query)),
    vscode.commands.registerCommand("codemap.showMapForFile", (uri?: vscode.Uri) => showMapForFile(context, graph, uri)),
    // Used by the "Mở map" button under @codemap answers: the nodes the answer cited.
    vscode.commands.registerCommand("codemap.showMapOf", async (args: { ids: number[]; focus: number[]; title: string }) => {
      const retriever = await graph.ready();
      if (retriever && args?.ids?.length) MapPanel.show(context, graph, args.title, retriever.mapAmong(args.ids, args.focus));
    }),
  );
  void refreshStatus();
}

async function showMap(context: vscode.ExtensionContext, graph: GraphService, query?: string): Promise<void> {
  const retriever = await graph.ready();
  if (!retriever) {
    void vscode.window.showWarningMessage(`Code Map: no ${DATABASE_NAME} loaded. Run the extract first.`);
    return;
  }
  const text =
    query ??
    (await vscode.window.showInputBox({
      prompt: "Bảng, API, màn hình, job hoặc thuật ngữ nghiệp vụ",
      placeHolder: "ORDER_HEADER · hủy đơn hàng · POST /api/orders",
    }));
  if (!text?.trim()) return;
  const found = retriever.findNodes(text, 15);
  if (!found.length) {
    void vscode.window.showInformationMessage(`Code Map: nothing matches "${text}".`);
    return;
  }
  let seed = found[0];
  if (found.length > 1 && query === undefined) {
    const picked = await vscode.window.showQuickPick(
      found.map((node) => ({
        label: node.name,
        description: node.type,
        detail: [node.names, node.file && `${node.file}${node.line ? `:${node.line}` : ""}`].filter(Boolean).join("  ·  "),
        node,
      })),
      { placeHolder: "Vẽ map quanh node nào?", matchOnDescription: true, matchOnDetail: true },
    );
    if (!picked) return;
    seed = picked.node;
  }
  MapPanel.show(context, graph, seed.name, retriever.mapGraph([seed.id], { hops: 2, maxNodes: 60 }));
}

async function showMapForFile(context: vscode.ExtensionContext, graph: GraphService, uri?: vscode.Uri): Promise<void> {
  const retriever = await graph.ready();
  const file = uri?.fsPath ?? vscode.window.activeTextEditor?.document.uri.fsPath;
  if (!retriever || !file) return;
  const declared = retriever.retrieve("", { mode: "file", file: graph.toGraphPath(file), maxNodes: 20, hops: 1 }).seeds;
  if (!declared.length) {
    void vscode.window.showInformationMessage(`Code Map: the graph has nothing declared in ${path.basename(file)}.`);
    return;
  }
  MapPanel.show(context, graph, path.basename(file), retriever.mapGraph(declared.map((node) => node.id), { hops: 1, maxNodes: 60 }));
}

async function runExtract(graph: GraphService, refreshStatus: () => Promise<void>): Promise<void> {
  const settings = vscode.workspace.getConfiguration("codemap.extract");
  let configPath = settings.get<string>("configPath", "").trim();
  if (!configPath) {
    const picked = await vscode.window.showOpenDialog({
      canSelectMany: false,
      filters: { "Extract config": ["json"] },
      openLabel: "Use this extract config",
    });
    if (!picked?.length) return;
    configPath = picked[0].fsPath;
    await settings.update("configPath", configPath, vscode.ConfigurationTarget.Workspace);
  }
  configPath = expandWorkspace(configPath);
  const engine = expandWorkspace(settings.get<string>("enginePath", "").trim() || findEngine() || "");
  if (!engine || !fs.existsSync(path.join(engine, "code_tree_exporter"))) {
    void vscode.window.showErrorMessage(
      "Code Map: set 'codemap.extract.enginePath' to the folder that contains code_tree_exporter (the code-map repository).",
    );
    return;
  }
  const python = settings.get<string>("python", "python");
  const task = new vscode.Task(
    { type: "shell", task: "codemap-extract" },
    vscode.TaskScope.Workspace,
    "Extract",
    "Code Map",
    new vscode.ShellExecution(python, ["-m", "code_tree_exporter", "extract", "--config", configPath], { cwd: engine }),
  );
  task.presentationOptions = { reveal: vscode.TaskRevealKind.Always, panel: vscode.TaskPanelKind.Dedicated };
  const execution = await vscode.tasks.executeTask(task);
  const listener = vscode.tasks.onDidEndTaskProcess(async (event) => {
    if (event.execution !== execution) return;
    listener.dispose();
    if (event.exitCode === 0) {
      await graph.reload();
      await refreshStatus();
      void vscode.window.showInformationMessage("Code Map: extract finished, graph reloaded. Ask with @codemap in Chat.");
    } else {
      void vscode.window.showErrorMessage(`Code Map: extract failed (exit code ${event.exitCode}). See the terminal.`);
    }
  });
}

function findEngine(): string | undefined {
  // The code-map repository keeps the engine in extractor/.
  return vscode.workspace.workspaceFolders
    ?.flatMap((folder) => [path.join(folder.uri.fsPath, "extractor"), folder.uri.fsPath])
    .find((folder) => fs.existsSync(path.join(folder, "code_tree_exporter")));
}

export function deactivate(): void {}
