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

if (process.argv.includes("--watch")) {
  await (await context(options)).watch();
} else {
  await build(options);
}
