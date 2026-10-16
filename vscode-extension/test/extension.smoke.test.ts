// Loads the bundled dist/extension.js against a minimal fake `vscode` module and
// drives the chat participant and the agent tools the way VS Code would.
import { strict as assert } from "node:assert";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { before, describe, it } from "node:test";

/* eslint-disable @typescript-eslint/no-explicit-any */
const repo = path.resolve(__dirname, "..", "..", "..");
const extensionRoot = path.resolve(__dirname, "..", "..");
const registered: { participant?: any; tools: Map<string, any>; commands: Map<string, any> } = {
  tools: new Map(),
  commands: new Map(),
};
let settings: Record<string, unknown> = {};

class EventEmitter {
  private listeners: ((value: unknown) => void)[] = [];
  event = (listener: (value: unknown) => void) => {
    this.listeners.push(listener);
    return { dispose() {} };
  };
  fire(value?: unknown) {
    for (const listener of this.listeners) listener(value);
  }
  dispose() {}
}
class Uri {
  constructor(readonly fsPath: string) {}
  static file(file: string) {
    return new Uri(file);
  }
}
class Position {
  constructor(readonly line: number, readonly character: number) {}
}
class Location {
  constructor(readonly uri: Uri, readonly range: Position) {}
}
class LanguageModelTextPart {
  constructor(readonly value: string) {}
}
class LanguageModelToolResult {
  constructor(readonly content: LanguageModelTextPart[]) {}
}

const fakeVscode = {
  EventEmitter,
  Uri,
  Position,
  Location,
  LanguageModelTextPart,
  LanguageModelToolResult,
  ThemeIcon: class {
    constructor(readonly id: string) {}
  },
  RelativePattern: class {
    constructor(readonly base: Uri, readonly pattern: string) {}
  },
  StatusBarAlignment: { Left: 1 },
  ConfigurationTarget: { Global: 1, Workspace: 2 },
  ChatRequestTurn: class {},
  ChatResponseTurn: class {},
  ChatResponseMarkdownPart: class {},
  LanguageModelError: class extends Error {},
  LanguageModelChatMessage: { User: (content: string) => ({ role: "user", content }), Assistant: (content: string) => ({ role: "assistant", content }) },
  window: {
    createOutputChannel: () => ({ appendLine() {}, show() {}, dispose() {} }),
    createStatusBarItem: () => ({ show() {}, dispose() {} }),
    showErrorMessage: async (message: string) => {
      throw new Error(`unexpected error message: ${message}`);
    },
    showWarningMessage: async () => undefined,
    showInformationMessage: async () => undefined,
    activeTextEditor: undefined as any,
  },
  workspace: {
    workspaceFolders: [{ uri: Uri.file(path.join(repo, "sample-source")) }],
    getConfiguration: (section: string) => ({
      get: (key: string, fallback: unknown) => settings[`${section}.${key}`] ?? fallback,
      update: async () => undefined,
    }),
    findFiles: async () => [],
    createFileSystemWatcher: () => ({ onDidChange() {}, onDidCreate() {}, dispose() {} }),
  },
  chat: {
    createChatParticipant: (_id: string, handler: unknown) => {
      registered.participant = { handler };
      return registered.participant;
    },
  },
  lm: {
    registerTool: (name: string, tool: unknown) => {
      registered.tools.set(name, tool);
      return { dispose() {} };
    },
  },
  commands: {
    registerCommand: (name: string, callback: unknown) => {
      registered.commands.set(name, callback);
      return { dispose() {} };
    },
    executeCommand: async () => undefined,
  },
  tasks: {},
};

function loadExtension(): any {
  const Module = require("node:module");
  const original = Module._load;
  Module._load = function (request: string, ...rest: unknown[]) {
    return request === "vscode" ? fakeVscode : original.call(this, request, ...rest);
  };
  try {
    return require(path.join(extensionRoot, "dist", "extension.js"));
  } finally {
    Module._load = original;
  }
}

describe("extension (bundled, fake VS Code host)", () => {
  before(() => {
    const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "codemap-smoke-")), "codemap-rag.sqlite");
    execFileSync(process.env.PYTHON ?? "python3", [
      "-m", "code_tree_exporter", "rag-export", "--graph", path.join(repo, "output", "demo"),
      "--input-data", path.join(repo, "input-data"), "--source-root", path.join(repo, "sample-source"), "--out", out,
    ], { cwd: repo, stdio: "pipe" });
    settings = { "codemap.databasePath": out };
    const storage = fs.mkdtempSync(path.join(os.tmpdir(), "codemap-storage-"));
    loadExtension().activate({
      subscriptions: [],
      globalStorageUri: Uri.file(storage),
      asAbsolutePath: (relative: string) => path.join(extensionRoot, relative),
    });
  });

  it("registers the participant, both tools and the commands", () => {
    assert.ok(registered.participant);
    assert.deepEqual([...registered.tools.keys()].sort(), ["codemap_file_context", "codemap_query"]);
    for (const command of ["codemap.runExtract", "codemap.reload", "codemap.selectDatabase", "codemap.status"]) {
      assert.ok(registered.commands.has(command), command);
    }
  });

  it("answers @codemap with graph facts, the model's text and file references", async () => {
    const sent: any[] = [];
    const markdown: string[] = [];
    const references: any[] = [];
    const result = await registered.participant.handler(
      {
        prompt: "hủy đơn hàng ghi vào bảng nào?",
        command: undefined,
        model: {
          sendRequest: async (messages: any[]) => {
            sent.push(...messages);
            return { text: (async function* () { yield "Trả lời [C1]"; })() };
          },
        },
      },
      { history: [] },
      { markdown: (value: string) => markdown.push(value), progress() {}, reference: (value: any) => references.push(value), button() {} },
      { isCancellationRequested: false },
    );
    assert.match(sent[0].content, /\[C1\] Cancel order \/ Hủy đơn hàng/);
    assert.match(sent[0].content, /CancelOrder/);
    assert.equal(sent.at(-1).content, "hủy đơn hàng ghi vào bảng nào?");
    assert.deepEqual(markdown, ["Trả lời [C1]"]);
    assert.ok(references.length > 0);
    assert.ok(references.every((reference) => fs.existsSync(reference.uri.fsPath)), "references resolve to real files");
    assert.ok(result.metadata);
  });

  it("explains the active file with /explain", async () => {
    const file = path.join(repo, "sample-source", "order-api", "OrderFlow.cs");
    fakeVscode.window.activeTextEditor = { document: { uri: Uri.file(file) }, selection: { isEmpty: true } };
    const sent: any[] = [];
    await registered.participant.handler(
      { prompt: "", command: "explain", model: { sendRequest: async (messages: any[]) => (sent.push(...messages), { text: (async function* () {})() }) } },
      { history: [] },
      { markdown() {}, progress() {}, reference() {}, button() {} },
      { isCancellationRequested: false },
    );
    assert.match(sent[0].content, /@ order-api\/OrderFlow\.cs/);
  });

  it("serves the agent tools", async () => {
    const query = await registered.tools.get("codemap_query").invoke({ input: { question: "ORDER_HEADER", mode: "impact" } });
    assert.match(query.content[0].value, /WRITES_TO \(INSERT\)/);
    const file = await registered.tools.get("codemap_file_context").invoke({ input: { path: "order-api/OrderFlow.cs" } });
    assert.match(file.content[0].value, /API_OPERATION/);
  });
});
