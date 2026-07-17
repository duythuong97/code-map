const fs = require("fs");
const path = require("path");

const venvRoot = path.join(__dirname, ".venv");
const venvLib = path.join(venvRoot, "lib");
const winSitePackages = path.join(venvRoot, "Lib", "site-packages");
const winVenvPython = path.join(venvRoot, "Scripts", "python.exe");
const unixVenvPython = path.join(venvRoot, "bin", "python");
const appConfigPath = path.join(__dirname, "code-map.config.json");
const rootConfig = JSON.parse(fs.readFileSync(appConfigPath, "utf8"));
const appConfig = { ...rootConfig.api, db: rootConfig.db };

function resolvePythonPath(configuredPython) {
  if (configuredPython) {
    if (configuredPython.includes("/") || configuredPython.includes("\\")) {
      return path.resolve(__dirname, configuredPython);
    }
    return configuredPython;
  }

  if (process.platform === "win32") {
    return fs.existsSync(winVenvPython) ? winVenvPython : "python";
  }

  return fs.existsSync(unixVenvPython) ? unixVenvPython : "python3";
}

const pythonPath = resolvePythonPath(appConfig.python);
const unixSitePackages = fs.existsSync(venvLib)
  ? fs
      .readdirSync(venvLib)
      .filter((name) => name.startsWith("python"))
      .map((name) => path.join(venvLib, name, "site-packages"))
      .find((dir) => fs.existsSync(dir))
  : "";
const sitePackages = unixSitePackages || (fs.existsSync(winSitePackages) ? winSitePackages : "");
const bootstrap = [
  "import runpy, sys",
  ...(sitePackages ? [`sys.path.insert(0, ${JSON.stringify(sitePackages)})`] : []),
  `sys.path.insert(0, ${JSON.stringify(__dirname)})`,
  `sys.argv = ["waitress", "--listen=${appConfig.host}:${appConfig.port}", "api.app:app"]`,
  'runpy.run_module("waitress", run_name="__main__")',
].join("; ");
const disableSite = Boolean(appConfig.python_no_site && sitePackages);
const pythonArgs = [
  ...(disableSite ? ["-S"] : []),
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
      exp_backoff_restart_delay: 1000,
      min_uptime: "5s",
      max_restarts: 10,
      max_memory_restart: "512M",
      watch: false,
    },
  ],
};
