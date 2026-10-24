import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createHash } from "node:crypto";
import { mkdir, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

interface StreamRecord {
  version: 1;
  session_file: string;
  role: "assistant";
  markdown: string;
  streaming: boolean;
  updated_at: number;
}

function streamDirectory(): string {
  return process.env.XDG_RUNTIME_DIR
    ? join(process.env.XDG_RUNTIME_DIR, "herdr-web-pi-streams")
    : join(tmpdir(), `herdr-web-pi-streams-${process.getuid?.() ?? "user"}`);
}

function streamPath(sessionFile: string): string {
  const digest = createHash("sha256").update(sessionFile).digest("hex");
  return join(streamDirectory(), `${digest}.json`);
}

function paneMappingPath(apiSocket: string, paneId: string): string {
  const digest = createHash("sha256").update(apiSocket).update("\0").update(paneId).digest("hex");
  return join(streamDirectory(), `pane-${digest}.json`);
}

async function writeAtomic(destination: string, value: unknown): Promise<void> {
  await mkdir(streamDirectory(), { recursive: true, mode: 0o700 });
  const temporary = `${destination}.${process.pid}.tmp`;
  await writeFile(temporary, JSON.stringify(value), { encoding: "utf8", mode: 0o600 });
  await rename(temporary, destination);
}

function messageText(message: unknown): string {
  if (!message || typeof message !== "object") return "";
  const content = (message as { content?: unknown }).content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((block): block is { type: "text"; text: string } =>
      !!block && typeof block === "object"
      && (block as { type?: unknown }).type === "text"
      && typeof (block as { text?: unknown }).text === "string")
    .map((block) => block.text)
    .join("\n\n");
}

export default function herdrWebStream(pi: ExtensionAPI) {
  let currentPath: string | undefined;
  let currentMappingPath: string | undefined;
  let writeSequence = Promise.resolve();
  let lastWrite = 0;
  let pendingTimer: NodeJS.Timeout | undefined;
  let pendingRecord: StreamRecord | undefined;

  function removeCurrent(removeMapping = false): void {
    if (pendingTimer) clearTimeout(pendingTimer);
    pendingTimer = undefined;
    pendingRecord = undefined;
    const paths = [currentPath, removeMapping ? currentMappingPath : undefined].filter(
      (path): path is string => !!path,
    );
    currentPath = undefined;
    if (removeMapping) currentMappingPath = undefined;
    if (paths.length) {
      writeSequence = writeSequence.then(
        () => Promise.all(paths.map((path) => rm(path, { force: true }))).then(() => undefined),
      ).catch(() => {});
    }
  }

  function writeRecord(record: StreamRecord): void {
    pendingRecord = record;
    const delay = Math.max(0, 40 - (Date.now() - lastWrite));
    if (pendingTimer) return;
    pendingTimer = setTimeout(() => {
      pendingTimer = undefined;
      const next = pendingRecord;
      pendingRecord = undefined;
      if (!next || !currentPath) return;
      const destination = currentPath;
      writeSequence = writeSequence.then(async () => {
        await writeAtomic(destination, next);
        lastWrite = Date.now();
      }).catch(() => {});
    }, delay);
  }

  pi.on("session_start", (_event, ctx) => {
    removeCurrent(true);
    const sessionFile = ctx.sessionManager.getSessionFile();
    if (!sessionFile) return;
    currentPath = streamPath(sessionFile);
    const apiSocket = process.env.HERDR_SOCKET_PATH;
    const paneId = process.env.HERDR_PANE_ID;
    if (apiSocket && paneId) {
      currentMappingPath = paneMappingPath(apiSocket, paneId);
      const destination = currentMappingPath;
      writeSequence = writeSequence.then(() => writeAtomic(destination, {
        version: 1,
        api_socket: apiSocket,
        pane_id: paneId,
        session_file: sessionFile,
        updated_at: Date.now(),
      })).catch(() => {});
    }
  });

  pi.on("message_update", (event, ctx) => {
    const sessionFile = ctx.sessionManager.getSessionFile();
    if (!sessionFile || event.message.role !== "assistant") return;
    currentPath = streamPath(sessionFile);
    writeRecord({
      version: 1,
      session_file: sessionFile,
      role: "assistant",
      markdown: messageText(event.message),
      streaming: true,
      updated_at: Date.now(),
    });
  });

  pi.on("message_end", (event) => {
    if (event.message.role === "assistant") removeCurrent();
  });
  pi.on("session_shutdown", () => removeCurrent(true));
}
