import * as vscode from "vscode";
import * as cp from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as crypto from "crypto";

type AgentEvent = { id?: string; type: "ready" | "text" | "tool" | "error" | "done"; text?: string };

/** Owns the Python chat agent process (src/chat_server.py, JSON lines over stdio). */
class AgentBridge {
  private proc: cp.ChildProcessWithoutNullStreams | undefined;
  private buffer = "";

  constructor(private readonly output: vscode.OutputChannel, private readonly onEvent: (e: AgentEvent) => void) {}

  get running(): boolean {
    return this.proc !== undefined;
  }

  send(request: object, repoPath: string, uvPath: string): void {
    if (!this.proc) {
      this.start(repoPath, uvPath);
    }
    this.proc!.stdin.write(JSON.stringify(request) + "\n");
  }

  stop(): void {
    this.proc?.kill();
    this.proc = undefined;
  }

  private start(repoPath: string, uvPath: string): void {
    this.output.appendLine(`Starting agent: ${uvPath} run python -m src.chat_server (cwd ${repoPath})`);
    const proc = cp.spawn(uvPath, ["run", "python", "-m", "src.chat_server"], {
      cwd: repoPath,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
    });
    this.proc = proc;
    this.buffer = "";
    // A failed spawn or a dead child turns writes into EPIPE; without a listener
    // that error would be thrown in the extension host. `exit`/`error` report it.
    proc.stdin.on("error", (err) => this.output.appendLine(`[stdin] ${err.message}`));
    proc.stdout.setEncoding("utf8");
    proc.stdout.on("data", (chunk: string) => {
      this.buffer += chunk;
      let nl: number;
      while ((nl = this.buffer.indexOf("\n")) >= 0) {
        const line = this.buffer.slice(0, nl).trim();
        this.buffer = this.buffer.slice(nl + 1);
        if (!line) continue;
        try {
          this.onEvent(JSON.parse(line));
        } catch {
          this.output.appendLine(`[stdout] ${line}`);
        }
      }
    });
    proc.stderr.setEncoding("utf8");
    proc.stderr.on("data", (chunk: string) => this.output.append(chunk));
    proc.on("error", (err) => {
      if (this.proc === proc) this.proc = undefined; // next message retries the spawn
      this.onEvent({ type: "error", text: `Cannot start agent (${uvPath}): ${err.message}` });
      this.onEvent({ type: "done" });
    });
    proc.on("exit", (code, signal) => {
      this.output.appendLine(`Agent exited (code ${code}, signal ${signal})`);
      if (this.proc === proc) {
        this.proc = undefined;
        this.onEvent({ type: "done" });
      }
    });
  }
}

class ChatViewProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private threadId = crypto.randomUUID();
  private readonly bridge: AgentBridge;

  constructor(private readonly context: vscode.ExtensionContext, output: vscode.OutputChannel) {
    this.bridge = new AgentBridge(output, (event) => this.post(event));
  }

  dispose(): void {
    this.bridge.stop();
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.html = this.html(view.webview);
    view.webview.onDidReceiveMessage((msg) => {
      if (msg.type === "send" && typeof msg.text === "string") this.ask(msg.text);
      if (msg.type === "stop") this.stop();
      if (msg.type === "newChat") this.newChat();
      if (msg.type === "ready") this.post({ type: "skills", items: this.skills() });
    });
  }

  newChat(): void {
    // A new chat must not keep receiving the old turn's events.
    this.bridge.stop();
    this.threadId = crypto.randomUUID();
    this.post({ type: "clear" });
  }

  stop(): void {
    // The agent runs one turn at a time, so stopping means killing the process;
    // conversation memory lives in that process and goes with it.
    if (this.bridge.running) {
      this.bridge.stop();
      this.threadId = crypto.randomUUID();
      this.post({ type: "error", text: "Đã dừng. Tin nhắn tiếp theo sẽ bắt đầu cuộc trò chuyện mới." });
      // bridge.stop() detaches the process first, so its exit sends no "done".
      this.post({ type: "done" });
    }
  }

  private ask(text: string): void {
    const workspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!workspace) {
      this.post({ type: "error", text: "Open a folder first: the agent looks for projects inside the workspace." });
      this.post({ type: "done" });
      return;
    }
    const repoPath = this.repoPath();
    if (!repoPath) {
      this.post({
        type: "error",
        text: "Set `unittestAgent.repoPath` to the deep-agents repo (the folder containing src/chat_server.py and .env).",
      });
      this.post({ type: "done" });
      return;
    }
    const uvPath = vscode.workspace.getConfiguration("unittestAgent").get<string>("uvPath") || "uv";
    this.bridge.send({ id: crypto.randomUUID(), thread_id: this.threadId, workspace, message: text }, repoPath, uvPath);
  }

  private repoPath(): string | undefined {
    const configured = vscode.workspace.getConfiguration("unittestAgent").get<string>("repoPath")?.trim();
    const candidate = configured || path.resolve(this.context.extensionPath, "..");
    return fs.existsSync(path.join(candidate, "src", "chat_server.py")) ? candidate : undefined;
  }

  /** Skills offered as "/" commands: name + description from each SKILL.md frontmatter. */
  private skills(): { name: string; description: string }[] {
    const repoPath = this.repoPath();
    if (!repoPath) return [];
    const dir = path.join(repoPath, "skills");
    const items: { name: string; description: string }[] = [];
    for (const entry of fs.existsSync(dir) ? fs.readdirSync(dir).sort() : []) {
      const file = path.join(dir, entry, "SKILL.md");
      if (!fs.existsSync(file)) continue;
      const front = /^---\r?\n([\s\S]*?)\r?\n---/.exec(fs.readFileSync(file, "utf8"))?.[1] ?? "";
      const description = /^description:\s*(.+)$/m.exec(front)?.[1].trim() ?? "";
      // The command is the folder name: that is what src/chat.py looks up.
      items.push({ name: entry, description });
    }
    return items;
  }

  private post(message: object): void {
    this.view?.webview.postMessage(message);
  }

  private html(webview: vscode.Webview): string {
    const nonce = crypto.randomBytes(16).toString("hex");
    const asset = (name: string) =>
      webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, "media", name));
    return /* html */ `<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${webview.cspSource} data:; style-src ${webview.cspSource}; font-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${asset("chat.css")}">
</head>
<body>
<div id="log"></div>
<div id="composer">
  <div class="box">
    <textarea id="input" rows="1" placeholder="Nhắn cho agent…"></textarea>
    <button id="send" title="Gửi"></button>
  </div>
  <div class="hint">Enter để gửi · Shift+Enter xuống dòng · / để xem lệnh</div>
</div>
<script nonce="${nonce}" src="${asset("marked.umd.js")}"></script>
<script nonce="${nonce}" src="${asset("chat.js")}"></script>
</body>
</html>`;
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Unit-test Agent");
  const provider = new ChatViewProvider(context, output);
  context.subscriptions.push(
    output,
    provider,
    vscode.window.registerWebviewViewProvider("unittestAgent.chat", provider, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.commands.registerCommand("unittestAgent.newChat", () => provider.newChat()),
    vscode.commands.registerCommand("unittestAgent.stop", () => provider.stop()),
  );
}

export function deactivate(): void {}
