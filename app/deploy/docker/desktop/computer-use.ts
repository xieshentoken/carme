// The pinned upstream extension executes in the Bot's Linux desktop session.
// Carme supplies identity and permissions; no Pi model or credential is loaded.
import readline from "node:readline";
import extension from "@injaneity/pi-computer-use/extensions/computer-use.ts";

const tools = new Map<string, any>();
const shutdown: Array<() => Promise<void>> = [];
extension({
  registerTool: (tool: any) => tools.set(tool.name, tool),
  registerCommand: () => {},
  on: (name: string, callback: any) => {
    if (name === "session_shutdown") shutdown.push(callback);
  },
} as any);
const ctx = { cwd: process.env.CARME_COMPUTER_CWD || "/home/bot", hasUI: false, sessionManager: { getBranch: () => [] },
  ui: { notify: () => {} } };
for await (const line of readline.createInterface({ input: process.stdin })) {
  let request: any;
  try {
    request = JSON.parse(line);
    if (request.name === "help") {
      const entries = [...tools.values()].filter((tool) => !["launch_browser", "evaluate_browser"].includes(tool.name)
        && (!request.arguments?.name || request.arguments.name === tool.name));
      const text = JSON.stringify(entries.map(({ name, description, parameters }) => ({ name, description, parameters })));
      process.stdout.write(JSON.stringify({ id: request.id, result: { content: [{ type: "text", text }] } }) + "\n");
      continue;
    }
    const tool = tools.get(request.name);
    if (!tool || ["launch_browser", "evaluate_browser"].includes(request.name)) {
      throw new Error("computer_use_tool_denied");
    }
    const result = await tool.execute(request.id, request.arguments || {},
      AbortSignal.timeout(65000), undefined, ctx);
    process.stdout.write(JSON.stringify({ id: request.id, result }) + "\n");
  } catch (error) {
    process.stdout.write(JSON.stringify({ id: request?.id, error: String(error).slice(0, 2000) }) + "\n");
  }
}
for (const close of shutdown) await close();
