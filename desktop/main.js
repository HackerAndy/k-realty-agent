// The desktop shell — deliberately thin. It owns zero application logic: it
// spawns the SAME backend `poetry run agent-web` starts (interfaces/rest_server.py,
// serving frontend/'s Vite build) as a child process, waits for it to answer,
// and points a BrowserWindow at it. The renderer is the ordinary web app —
// callTool() still does fetch('/api/tool/...') unchanged — so there is no
// second frontend to maintain and no IPC bridge to keep in sync with the REST
// API. See interfaces/rest_server.py's module docstring for the transport-
// swap-point this reuses.
//
// Packaging a real Python interpreter/venv into a distributable app (so an
// operator doesn't need Poetry installed) is explicitly NOT done here yet —
// this dev-launches via `poetry run agent-web` on PATH. Tracked as follow-up.

const { app, BrowserWindow, dialog } = require("electron");
const { spawn } = require("node:child_process");
const path = require("node:path");

const REPO_ROOT = path.join(__dirname, "..");
const HOST = "127.0.0.1";
const PORT = 8765;
const BASE_URL = `http://${HOST}:${PORT}`;
const HEALTH_TIMEOUT_MS = 30_000;
const HEALTH_POLL_MS = 300;

let backend = null;         // the child process, if THIS app started one
let mainWindow = null;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function isBackendUp() {
  try {
    const res = await fetch(BASE_URL, { signal: AbortSignal.timeout(1000) });
    return res.ok;
  } catch {
    return false;
  }
}

async function waitForBackend(deadline) {
  while (Date.now() < deadline) {
    if (await isBackendUp()) return true;
    await sleep(HEALTH_POLL_MS);
  }
  return false;
}

// If something is already answering on :8765 (a dev server started by hand,
// or `poetry run agent-web` running separately), use it as-is rather than
// spawning a second one on top of it — and don't kill someone else's process
// on quit either (see app.on("before-quit")).
async function ensureBackend() {
  if (await isBackendUp()) {
    console.log(`[desktop] reusing an already-running backend at ${BASE_URL}`);
    return;
  }

  console.log("[desktop] starting backend: poetry run agent-web");
  backend = spawn("poetry", ["run", "agent-web"], {
    cwd: REPO_ROOT,
    stdio: ["ignore", "pipe", "pipe"],
  });
  backend.stdout.on("data", (chunk) => process.stdout.write(`[backend] ${chunk}`));
  backend.stderr.on("data", (chunk) => process.stderr.write(`[backend] ${chunk}`));
  backend.on("error", (err) => {
    console.error("[desktop] failed to spawn backend:", err);
  });
  backend.on("exit", (code, signal) => {
    console.log(`[desktop] backend exited (code=${code} signal=${signal})`);
    backend = null;
  });

  const up = await waitForBackend(Date.now() + HEALTH_TIMEOUT_MS);
  if (!up) {
    throw new Error(
      `Backend did not answer at ${BASE_URL} within ${HEALTH_TIMEOUT_MS / 1000}s. ` +
      "Is Poetry installed and on PATH, and has 'poetry install' been run in " +
      `${REPO_ROOT}?`
    );
  }
  console.log(`[desktop] backend is up at ${BASE_URL}`);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    title: "K-Realty",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.loadURL(BASE_URL);
  mainWindow.on("closed", () => { mainWindow = null; });
}

app.whenReady().then(async () => {
  try {
    await ensureBackend();
  } catch (err) {
    dialog.showErrorBox("K-Realty couldn't start", String(err.message || err));
    app.quit();
    return;
  }
  createWindow();

  app.on("activate", () => {
    // macOS convention: clicking the dock icon with no windows open reopens one.
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  // Unlike a typical Mac app, this one owns a backend process and isn't meant
  // to sit invisibly in the dock once its only window is gone — quit on every
  // platform rather than following the "stay open until Cmd+Q" convention.
  app.quit();
});

app.on("before-quit", () => {
  // Only kill the backend if THIS app started it — never a server the
  // operator was already running by hand (see ensureBackend's reuse path).
  if (backend) {
    console.log("[desktop] stopping backend");
    backend.kill();
  }
});
