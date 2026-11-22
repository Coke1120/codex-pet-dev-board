import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const python = fileURLToPath(new URL("./bin/python", import.meta.url));
const monitor = fileURLToPath(new URL("./codex_pet_monitor.py", import.meta.url));

function writeEvent(event, runId) {
  return new Promise((resolve) => {
    const child = spawn(python, [monitor, "hook", "openclaw"], {
      stdio: ["pipe", "ignore", "ignore"],
      timeout: 1000,
      killSignal: "SIGKILL",
    });
    child.on("error", () => {});
    child.once("close", resolve);
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify({ hook_event_name: event, run_id: runId }));
  });
}

export default {
  id: "codex-pet-monitor",
  name: "Codex Pet Monitor",
  description: "Displays local OpenClaw run state on Codex Pet.",
  register(api) {
    const pending = new Map();
    function observe(event) {
      // Only use the typed run identity. The conversation event is ignored.
      return (_event, context) => {
        const runId = context?.runId;
        if (typeof runId !== "string" || !runId.trim() || runId.length > 1024) return;
        const previous = pending.get(runId) ?? Promise.resolve();
        // Keep begin/end writes ordered per run without making OpenClaw wait.
        const next = previous.then(() => writeEvent(event, runId)).catch(() => {});
        pending.set(runId, next);
        void next.then(() => {
          if (pending.get(runId) === next) pending.delete(runId);
        });
      };
    }
    api.on("before_agent_run", observe("before_agent_run"));
    api.on("agent_end", observe("agent_end"));
  },
};
