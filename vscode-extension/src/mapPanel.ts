import * as crypto from "node:crypto";
import * as vscode from "vscode";
import { MapGraph } from "./core/retriever";
import { GraphService } from "./graphService";

/** One map panel per window; new requests replace its graph. */
export class MapPanel {
  private static current?: MapPanel;
  private ready = false;
  private pending?: { graph: MapGraph; merge: boolean };

  static show(context: vscode.ExtensionContext, graph: GraphService, title: string, map: MapGraph): void {
    const panel = (MapPanel.current ??= new MapPanel(context, graph));
    panel.panel.title = `Code Map: ${title}`;
    panel.panel.reveal(vscode.ViewColumn.Beside, true);
    panel.post({ type: "title", title });
    panel.send(map, false);
  }

  private readonly panel: vscode.WebviewPanel;

  private constructor(private readonly context: vscode.ExtensionContext, private readonly graph: GraphService) {
    this.panel = vscode.window.createWebviewPanel("codemap.map", "Code Map", { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true }, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [vscode.Uri.joinPath(context.extensionUri, "dist")],
    });
    this.panel.webview.html = this.html();
    this.panel.onDidDispose(() => (MapPanel.current = undefined));
    this.panel.webview.onDidReceiveMessage((message) => void this.handle(message));
  }

  private send(graph: MapGraph, merge: boolean): void {
    if (!this.ready) {
      this.pending = { graph, merge };
      return;
    }
    this.post({ type: "graph", graph, merge });
  }

  private post(message: unknown): void {
    void this.panel.webview.postMessage(message);
  }

  private async handle(message: { type: string; id?: number; mode?: string; file?: string; line?: number; name?: string }) {
    const retriever = await this.graph.ready();
    switch (message.type) {
      case "ready":
        this.ready = true;
        if (this.pending) this.send(this.pending.graph, this.pending.merge);
        this.pending = undefined;
        return;
      case "open": {
        const uri = message.file ? this.graph.resolveSource(message.file) : undefined;
        if (!uri) {
          void vscode.window.setStatusBarMessage(`Code Map: ${message.file} is not under the source root`, 4000);
          return;
        }
        const line = Math.max(0, (message.line ?? 1) - 1);
        await vscode.window.showTextDocument(uri, {
          viewColumn: vscode.ViewColumn.One,
          preserveFocus: true,
          preview: true,
          selection: new vscode.Range(line, 0, line, 0),
        });
        return;
      }
      case "expand":
        if (retriever && message.id) this.send(retriever.mapGraph([message.id], { hops: 1, maxNodes: 25 }), true);
        return;
      case "focus":
        if (retriever && message.id) {
          const flow = message.mode === "flow";
          this.send(retriever.mapGraph([message.id], { hops: flow ? 4 : 3, direction: flow ? "out" : "in", maxNodes: 80 }), false);
        }
        return;
      case "ask":
        await vscode.commands.executeCommand("workbench.action.chat.open", { query: `@codemap ${message.name ?? ""}` });
        return;
    }
  }

  private html(): string {
    const webview = this.panel.webview;
    const script = webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, "dist", "map.js"));
    const nonce = crypto.randomBytes(16).toString("base64");
    return mapHtml(webview.cspSource, script.toString(), nonce);
  }
}

export function mapHtml(cspSource: string, scriptUri: string, nonce: string): string {
  return `<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${cspSource} data:; style-src ${cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Map</title>
<style>
  body { margin: 0; height: 100vh; display: flex; flex-direction: column; background: var(--vscode-editor-background); color: var(--vscode-editor-foreground); font-family: var(--vscode-font-family); font-size: 12px; }
  header { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; padding: 6px 10px; border-bottom: 1px solid var(--vscode-panel-border, #3333); }
  header strong { font-size: 13px; margin-right: 6px; }
  button { background: var(--vscode-button-secondaryBackground, #3a3d41); color: var(--vscode-button-secondaryForeground, #fff); border: 0; padding: 4px 9px; border-radius: 3px; cursor: pointer; }
  button:hover { background: var(--vscode-button-secondaryHoverBackground, #45494e); }
  input[type=search] { background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border, transparent); padding: 3px 6px; min-width: 160px; }
  label { display: inline-flex; gap: 3px; align-items: center; }
  .swatch { display: inline-block; width: 10px; height: 3px; }
  #columns { display: flex; flex-wrap: wrap; gap: 4px 14px; padding: 4px 10px; opacity: 0.85; font-size: 11px; }
  #columns .layer { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
  #map { flex: 1; min-height: 0; }
  footer { display: flex; justify-content: space-between; gap: 12px; padding: 4px 10px; border-top: 1px solid var(--vscode-panel-border, #3333); opacity: 0.85; }
  #info { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
</head>
<body>
<header>
  <strong id="title">Code Map</strong>
  <input id="search" type="search" placeholder="Tìm trong map…" aria-label="Tìm trong map">
  <label><input type="checkbox" data-category="write" checked><span class="swatch" style="background:#e8833a"></span>Ghi</label>
  <label><input type="checkbox" data-category="read" checked><span class="swatch" style="background:#4c9be8"></span>Đọc</label>
  <label><input type="checkbox" data-category="call" checked><span class="swatch" style="background:#9aa4b2"></span>Gọi</label>
  <label><input type="checkbox" data-category="business" checked><span class="swatch" style="background:#b07fe0"></span>Batch</label>
  <label><input type="checkbox" data-category="structure"><span class="swatch" style="background:#6b7280"></span>Cấu trúc</label>
  <label><input type="checkbox" data-category="other"><span class="swatch" style="background:#8b95a3"></span>Khác</label>
  <button id="impact" title="Ai đọc/ghi/phụ thuộc vào node đang chọn">Impact</button>
  <button id="flow" title="Đi xuôi từ node đang chọn xuống dữ liệu">Flow</button>
  <button id="ask" title="Hỏi @codemap về node đang chọn">Hỏi</button>
  <button id="fit">Vừa khung</button>
</header>
<div id="columns"></div>
<div id="map"></div>
<footer><span id="info">Nhấp node để mở code · nhấp đúp để mở rộng</span><span id="status"></span></footer>
<script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
}
