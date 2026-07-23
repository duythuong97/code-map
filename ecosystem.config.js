const fs = require("fs");
const path = require("path");

const rootDir = __dirname;
const appConfigPath = path.join(rootDir, "configs", "code-map.config.json");
const rootConfig = JSON.parse(fs.readFileSync(appConfigPath, "utf8"));
const appConfig = { ...rootConfig.api, db: rootConfig.db };

function resolvePythonPath(configuredPython) {
  if (configuredPython) {
    if (configuredPython.includes("/") || configuredPython.includes("\\")) {
      return path.resolve(rootDir, configuredPython);
    }
    return configuredPython;
  }

  const venvPython = process.platform === "win32"
    ? path.join(rootDir, ".venv", "Scripts", "python.exe")
    : path.join(rootDir, ".venv", "bin", "python");

  if (fs.existsSync(venvPython)) {
    return venvPython;
  }

  return process.platform === "win32" ? "python" : "python3";
}

function resolvePythonArgs() {
  const args = [];
  if (appConfig.python_no_site) {
    args.push("-S");
  }
  args.push(path.join("application", "backend", "api", "server.py"));
  return args;
}

module.exports = {
  apps: [
    {
      name: appConfig.pm2_name || "code-map",
      cwd: rootDir,
      script: resolvePythonPath(appConfig.python),
      args: resolvePythonArgs(),
      interpreter: "none",
      env: {
        NODE_ENV: "production",
        CODE_MAP_CONFIG: appConfigPath,
        PYTHONPATH: rootDir,
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
