import { context, build } from "esbuild";
import { copyFileSync, mkdirSync } from "node:fs";

const options = {
  entryPoints: ["src/extension.ts"],
  bundle: true,
  outfile: "dist/extension.js",
  platform: "node",
  format: "cjs",
  target: "node20",
  sourcemap: true,
  // vscode is provided by the host; node:sqlite is optional and loaded at runtime.
  external: ["vscode", "node:sqlite"],
};

mkdirSync("dist", { recursive: true });
copyFileSync("node_modules/sql.js/dist/sql-wasm.wasm", "dist/sql-wasm.wasm");

// The map webview runs in the browser sandbox of the panel.
const webview = {
  entryPoints: ["src/webview/map.js"],
  bundle: true,
  outfile: "dist/map.js",
  platform: "browser",
  format: "iife",
  target: "es2020",
  minify: true,
};

if (process.argv.includes("--watch")) {
  await (await context(options)).watch();
  await (await context(webview)).watch();
} else {
  await build(options);
  await build(webview);
}
