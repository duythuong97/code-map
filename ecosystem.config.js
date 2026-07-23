const path = require("path");
require("dotenv").config({ path: path.join(__dirname, ".env"), override: true });

function required(name) {
  if (!process.env[name]) throw new Error(`Missing required environment variable: ${name}`);
  return process.env[name];
}

function absolute(name) {
  const value = required(name);
  if (!path.isAbsolute(value)) throw new Error(`${name} must be an absolute path: ${value}`);
  return value;
}

const projectRoot = absolute("CODE_MAP_PROJECT_ROOT");

module.exports = {
  apps: [
    {
      name: required("CODE_MAP_PM2_NAME"),
      script: path.join(projectRoot, ".venv", "bin", "python"),
      args: [path.join(projectRoot, "application", "backend", "api", "server.py")],
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
