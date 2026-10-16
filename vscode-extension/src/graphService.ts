import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import { GraphDb, openGraphDb } from "./core/db";
import { Retriever } from "./core/retriever";

export const DATABASE_NAME = "codemap-rag.sqlite";

/**
 * Owns the open graph database. The extension reads a private copy of
 * codemap-rag.sqlite so an open handle never blocks the next extract from
 * replacing the output folder (Windows refuses to rename open files).
 */
export class GraphService implements vscode.Disposable {
  private db?: GraphDb;
  private retriever?: Retriever;
  private source?: string;
  private loading?: Promise<Retriever | undefined>;
  private watcher?: vscode.FileSystemWatcher;
  private reloadTimer?: NodeJS.Timeout;
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;

  constructor(private readonly context: vscode.ExtensionContext, private readonly log: vscode.OutputChannel) {}

  get databasePath(): string | undefined {
    return this.source;
  }

  get engine(): string | undefined {
    return this.db?.engine;
  }

  /** The loaded retriever, loading the database on first use. */
  async ready(): Promise<Retriever | undefined> {
    if (this.retriever) return this.retriever;
    this.loading ??= this.load().finally(() => (this.loading = undefined));
    return this.loading;
  }

  async reload(): Promise<Retriever | undefined> {
    this.close();
    return this.ready();
  }

  private async load(): Promise<Retriever | undefined> {
    const source = await this.locate();
    if (!source) {
      this.log.appendLine(`No ${DATABASE_NAME} found. Run "Code Map: Run Extract" or "Code Map: Select Graph Database…".`);
      return undefined;
    }
    try {
      const copy = await this.privateCopy(source);
      this.db = await openGraphDb(copy, this.context.asAbsolutePath("dist"));
      this.retriever = new Retriever(this.db);
      this.source = source;
      this.watch(source);
      this.log.appendLine(`Loaded ${source} with ${this.db.engine}: ${JSON.stringify(this.retriever.stats())}`);
      this.changed.fire();
      return this.retriever;
    } catch (error) {
      this.log.appendLine(`Failed to open ${source}: ${error instanceof Error ? error.stack : String(error)}`);
      void vscode.window.showErrorMessage(`Code Map: cannot open ${source}: ${error instanceof Error ? error.message : error}`);
      this.close();
      return undefined;
    }
  }

  private async locate(): Promise<string | undefined> {
    const configured = vscode.workspace.getConfiguration("codemap").get<string>("databasePath", "").trim();
    if (configured) {
      const resolved = expandWorkspace(configured);
      const file = fs.existsSync(resolved) && fs.statSync(resolved).isDirectory() ? path.join(resolved, DATABASE_NAME) : resolved;
      return fs.existsSync(file) ? file : undefined;
    }
    const found = await vscode.workspace.findFiles(`**/${DATABASE_NAME}`, "**/node_modules/**", 20);
    const newest = found
      .map((uri) => ({ file: uri.fsPath, time: fs.statSync(uri.fsPath).mtimeMs }))
      .sort((a, b) => b.time - a.time)[0];
    return newest?.file;
  }

  private async privateCopy(source: string): Promise<string> {
    const storage = this.context.globalStorageUri.fsPath;
    await fs.promises.mkdir(storage, { recursive: true });
    const stat = await fs.promises.stat(source);
    const key = crypto.createHash("sha1").update(source).digest("hex").slice(0, 12);
    const copy = path.join(storage, `${key}-${stat.size}-${Math.floor(stat.mtimeMs)}.sqlite`);
    if (!fs.existsSync(copy)) {
      for (const old of await fs.promises.readdir(storage)) {
        if (old.startsWith(`${key}-`)) await fs.promises.rm(path.join(storage, old), { force: true });
      }
      await fs.promises.copyFile(source, copy + ".tmp");
      await fs.promises.rename(copy + ".tmp", copy);
    }
    return copy;
  }

  private watch(source: string): void {
    this.watcher?.dispose();
    this.watcher = vscode.workspace.createFileSystemWatcher(
      new vscode.RelativePattern(vscode.Uri.file(path.dirname(source)), DATABASE_NAME),
    );
    const schedule = () => {
      clearTimeout(this.reloadTimer);
      this.reloadTimer = setTimeout(() => void this.reload(), 1500);
    };
    this.watcher.onDidChange(schedule);
    this.watcher.onDidCreate(schedule);
  }

  /** Absolute file for an evidence path, trying the configured root, the extract root and the workspace. */
  resolveSource(relative: string): vscode.Uri | undefined {
    if (path.isAbsolute(relative)) return fs.existsSync(relative) ? vscode.Uri.file(relative) : undefined;
    for (const root of this.sourceRoots()) {
      const candidate = path.join(root, relative);
      if (fs.existsSync(candidate)) return vscode.Uri.file(candidate);
    }
    return undefined;
  }

  /** Path of a workspace file as the graph records it (relative to the source root, forward slashes). */
  toGraphPath(file: string): string {
    for (const root of this.sourceRoots()) {
      const relative = path.relative(root, file);
      if (relative && !relative.startsWith("..") && !path.isAbsolute(relative)) return relative.split(path.sep).join("/");
    }
    return path.basename(file);
  }

  private sourceRoots(): string[] {
    const roots: string[] = [];
    const configured = vscode.workspace.getConfiguration("codemap").get<string>("sourceRoot", "").trim();
    if (configured) roots.push(expandWorkspace(configured));
    const recorded = this.retriever?.meta.source_root;
    if (recorded) roots.push(recorded);
    for (const folder of vscode.workspace.workspaceFolders ?? []) {
      roots.push(folder.uri.fsPath, path.dirname(folder.uri.fsPath));
    }
    return [...new Set(roots)];
  }

  private close(): void {
    clearTimeout(this.reloadTimer);
    this.db?.close();
    this.db = undefined;
    this.retriever = undefined;
  }

  dispose(): void {
    this.close();
    this.watcher?.dispose();
    this.changed.dispose();
  }
}

export function expandWorkspace(value: string): string {
  const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? "";
  const expanded = value.replace(/\$\{workspaceFolder\}/g, folder);
  return path.isAbsolute(expanded) || !folder ? expanded : path.join(folder, expanded);
}
