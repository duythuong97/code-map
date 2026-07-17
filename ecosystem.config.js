const fs = require("fs");
const path = require("path");

const venvLib = path.join(__dirname, ".venv", "lib");
const appConfigPath = path.join(__dirname, "code-map.config.json");
const rootConfig = JSON.parse(fs.readFileSync(appConfigPath, "utf8"));
const appConfig = { ...rootConfig.api, db: rootConfig.db };
const pythonPath = appConfig.python.includes(path.sep)
  ? path.resolve(__dirname, appConfig.python)
  : appConfig.python;
const sitePackages = fs.existsSync(venvLib)
  ? fs
      .readdirSync(venvLib)
      .filter((name) => name.startsWith("python"))
      .map((name) => path.join(venvLib, name, "site-packages"))
      .find((dir) => fs.existsSync(dir))
  : "";
const bootstrap = [
  "import runpy, sys",
  `sys.path.insert(0, ${JSON.stringify(sitePackages)})`,
  `sys.path.insert(0, ${JSON.stringify(__dirname)})`,
  `sys.argv = ["waitress", "--listen=${appConfig.host}:${appConfig.port}", "api.app:app"]`,
  'runpy.run_module("waitress", run_name="__main__")',
].join("; ");
const pythonArgs = [
  ...(appConfig.python_no_site ? ["-S"] : []),
  "-c",
  bootstrap,
];

module.exports = {
  apps: [
    {
      name: appConfig.pm2_name,
      cwd: __dirname,
      script: pythonPath,
      args: pythonArgs,
      interpreter: "none",
      env: {
        NODE_ENV: "production",
        CODE_MAP_CONFIG: appConfigPath,
        PYTHONPATH: [sitePackages, __dirname]
          .filter(Boolean)
          .join(path.delimiter),
      },
      autorestart: true,
      max_memory_restart: "512M",
      watch: false,
    },
  ],
};
