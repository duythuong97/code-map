const fs = require("fs");
const path = require("path");

const projectRoot = path.join(__dirname, "..");

// Only the pm2 process name is read here; the backend loads .env (CODE_MAP_CONFIG, token) itself.
function pm2Name() {
  if (process.env.CODE_MAP_PM2_NAME) return process.env.CODE_MAP_PM2_NAME;
  try {
    const line = fs.readFileSync(path.join(projectRoot, ".env"), "utf8").match(/^\s*CODE_MAP_PM2_NAME\s*=\s*(.+?)\s*$/m);
    if (line) return line[1].replace(/^["']|["']$/g, "");
  } catch {
    // no .env: use the default name
  }
  return "code-map";
}

module.exports = {
  apps: [
    {
      name: pm2Name(),
      // The virtualenv interpreter: Scripts\python.exe on Windows, bin/python elsewhere.
      script: process.platform === "win32"
        ? path.join(projectRoot, ".venv", "Scripts", "python.exe")
        : path.join(projectRoot, ".venv", "bin", "python"),
      args: ["-m", "codemap", "serve"],
      cwd: projectRoot,
      interpreter: "none",
      env: { NODE_ENV: "production" },
      autorestart: true,
      exp_backoff_restart_delay: 1000,
      min_uptime: "5s",
      max_restarts: 10,
      max_memory_restart: "512M",
      watch: false,
    },
  ],
};
