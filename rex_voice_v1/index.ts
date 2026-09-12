import net from "node:net";
import fs from "node:fs";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { explicitHangupRequest, requestedAction, routeTurn } from "./routing.ts";

const socketPath = process.env.REX_VOICE_BRIDGE_SOCKET;
if (!socketPath) throw new Error("REX_VOICE_BRIDGE_SOCKET is required");

function bridgeRequest(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath);
    let buffer = "";
    socket.setTimeout(15000);
    socket.on("connect", () => {
      socket.write(JSON.stringify({ method, params }) + "\n");
    });
    socket.on("data", (data) => {
      buffer += data.toString();
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      try {
        const value = JSON.parse(buffer.slice(0, newline)) as Record<string, unknown>;
        if (value.ok !== true) reject(new Error(JSON.stringify(value)));
        else resolve(value);
      } catch (error) { reject(error); }
      socket.end();
    });
    socket.on("timeout", () => { socket.destroy(new Error("capability bridge timeout")); });
    socket.on("error", reject);
  });
}

const sessionId = process.env.REX_VOICE_SESSION_ID ?? "unknown";
const call = (operation: string, arguments_: Record<string, unknown>) =>
  bridgeRequest("capability", { session_id: sessionId, operation, arguments: arguments_ });


function textFromContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map((part) => {
    if (typeof part === "string") return part;
    if (part && typeof part === "object" && "text" in part) return String((part as { text?: unknown }).text ?? "");
    return "";
  }).join(" ");
}

function lastUserText(payload: unknown): string {
  if (!payload || typeof payload !== "object") return "";
  const messages = (payload as { messages?: unknown }).messages;
  if (!Array.isArray(messages)) return "";
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message && typeof message === "object" && (message as { role?: unknown }).role === "user") {
      return textFromContent((message as { content?: unknown }).content);
    }
  }
  return "";
}

function lastMessageIsUser(payload: unknown): boolean {
  if (!payload || typeof payload !== "object") return false;
  const messages = (payload as { messages?: unknown }).messages;
  if (!Array.isArray(messages) || messages.length === 0) return false;
  const last = messages[messages.length - 1];
  return Boolean(last && typeof last === "object" && (last as { role?: unknown }).role === "user");
}

function restrictPhoneHangup(payload: Record<string, unknown>, text: string): Record<string, unknown> {
  if (explicitHangupRequest(text)) return payload;
  const tools = payload.tools;
  if (!Array.isArray(tools)) return payload;
  return {
    ...payload,
    tools: tools.filter((tool) => {
      if (!tool || typeof tool !== "object") return true;
      const candidate = tool as { name?: unknown; function?: { name?: unknown } };
      return candidate.name !== "phone_hangup" && candidate.function?.name !== "phone_hangup";
    }),
  };
}

const PREPARED_TOPIC_CONTROL = /\[\[REX_PREPARED_TOPIC_CONTROL (\{[^\]]+\})\]\]/;

function preparedTopicControl(text: string): { topic_id: string; version: string } | undefined {
  const match = text.match(PREPARED_TOPIC_CONTROL);
  if (!match) return undefined;
  try {
    const value = JSON.parse(match[1]) as { topic_id?: unknown; version?: unknown };
    if (typeof value.topic_id !== "string" || typeof value.version !== "string") return undefined;
    return { topic_id: value.topic_id, version: value.version };
  } catch {
    return undefined;
  }
}

function stripPreparedTopicControl(content: unknown): unknown {
  if (typeof content === "string") return content.replace(PREPARED_TOPIC_CONTROL, "").trim();
  if (!Array.isArray(content)) return content;
  return content.map((part) => {
    if (part && typeof part === "object" && "text" in part && typeof (part as { text?: unknown }).text === "string") {
      return { ...(part as Record<string, unknown>), text: stripPreparedTopicControl((part as { text: string }).text) };
    }
    return part;
  });
}

function suppressPreparedTopicCapability(payload: Record<string, unknown>): Record<string, unknown> {
  const messages = payload.messages;
  const tools = payload.tools;
  return {
    ...payload,
    messages: Array.isArray(messages)
      ? messages.map((message) => {
        if (!message || typeof message !== "object" || (message as { role?: unknown }).role !== "user") return message;
        return { ...(message as Record<string, unknown>), content: stripPreparedTopicControl((message as { content?: unknown }).content) };
      })
      : messages,
    tools: Array.isArray(tools)
      ? tools.filter((tool) => {
        if (!tool || typeof tool !== "object") return true;
        const candidate = tool as { name?: unknown; function?: { name?: unknown } };
        return candidate.name !== "topic_activate" && candidate.function?.name !== "topic_activate";
      })
      : tools,
  };
}

function captureProviderPayload(payload: unknown): void {
  const path = process.env.REX_VOICE_PROVIDER_CAPTURE_PATH;
  if (!path) return;
  try {
    fs.appendFileSync(path, JSON.stringify(payload) + "\n", "utf8");
  } catch {
    // Diagnostics must never alter tool execution or turn behavior.
  }
}

async function executeCapability(
  ctx: { ui: { notify(message: string, type?: "info" | "warning" | "error"): void } },
  operation: string,
  arguments_: Record<string, unknown>,
) {
  ctx.ui.notify(`[Hermes] ${operation} requested; waiting for authoritative result.`, "info");
  try {
    const result = await call(operation, arguments_);
    ctx.ui.notify(`[Hermes] ${operation} completed: backend returned OK.`, "info");
    return result;
  } catch (error) {
    ctx.ui.notify(`[Hermes] ${operation} failed: ${String(error)}`, "error");
    throw error;
  }
}

export default function rexVoiceExtension(pi: ExtensionAPI) {
  let assignmentCapturePending = false;

  function assignmentCancellation(text: string): boolean {
    return /\b(?:cancel|never mind|nevermind|forget)\b.*\bassignment\b/i.test(text);
  }

  pi.on("session_start", () => {
    assignmentCapturePending = false;
  });

  pi.on("input", (event, ctx) => {
    // Explicit document actions get a narrow capability surface. Discussion
    // remains automatic with all tools available; this is not a global
    // tool-forcing policy and does not execute or parse assistant text.
    // Tool control belongs to ExtensionAPI (`pi`), not ExtensionContext (`ctx`).
    // This is a supported runtime operation and is applied before the request.
    const currentAction = requestedAction(event.text);
    if (explicitHangupRequest(event.text)) {
      // Session termination wins over any unfinished assignment continuation.
      // Do not let the current utterance become assignment content.
      assignmentCapturePending = false;
    } else if (assignmentCancellation(event.text)) {
      assignmentCapturePending = false;
    } else if (currentAction === "assignment_capture") {
      // Assignment setup is conversational: the user may announce the
      // assignment first and provide its actual request on the next turn.
      // Preserve that explicit intent until a native capture succeeds.
      assignmentCapturePending = true;
    }
    pi.setActiveTools(routeTurn(event.text, assignmentCapturePending).activeTools);
    return { action: "continue" };
  });
  pi.on("tool_execution_end", (event) => {
    if (event.toolName === "assignment_capture" && !event.isError) {
      assignmentCapturePending = false;
    }
  });
  pi.on("before_provider_request", (event) => {
    // The retry is injected by the host as a follow-up prompt, so it does not
    // emit another `input` event. Classify only the last user message here;
    // never inspect the system prompt or historical turns.
    const lastText = lastMessageIsUser(event.payload) ? lastUserText(event.payload) : "";
    const requested = lastText ? routeTurn(lastText, assignmentCapturePending).requested : undefined;
    const topicControl = lastText ? preparedTopicControl(lastText) : undefined;
    let providerPayload = restrictPhoneHangup(event.payload as Record<string, unknown>, lastText);
    if (topicControl && event.payload && typeof event.payload === "object") {
      providerPayload = suppressPreparedTopicCapability(providerPayload);
    }
    if (!requested || !event.payload || typeof event.payload !== "object") {
      captureProviderPayload(providerPayload);
      return topicControl ? providerPayload : undefined;
    }
    const payload = {
      ...providerPayload,
      tool_choice: { type: "function", function: { name: requested } },
    };
    captureProviderPayload(payload);
    return payload;
  });
  pi.registerTool({
    name: "retrieval",
    label: "Retrieval",
    description: "Search bounded authoritative note chunks when quick notes and the active prepared topic lack the requested detail, including prior decisions, stored project details, what we decided, and what notes say. The mode value must be exactly note (not search); note search prefers active-topic source references, widens once to the approved full-note corpus on a miss, and returns source paths, sections, chunks, scores, versions, and handles. It never searches the web automatically; use resource_manage open plus resource_read for an exact/full note.",
    parameters: Type.Object({ query: Type.String(), mode: Type.Union([Type.Literal("note"), Type.Literal("prepared"), Type.Literal("current_web")]), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 10 })) }),
    async execute(_id, params, _signal, _onUpdate, ctx) { return { content: [{ type: "text", text: JSON.stringify(await executeCapability(ctx, "retrieval", params)) }] }; },
  });
  pi.registerTool({
    name: "topic_activate",
    label: "Activate prepared topic",
    description: "Match the user's topic against local aliases and load one bounded prepared briefing into the current voice session. This does not search the full knowledge base.",
    parameters: Type.Object({ query: Type.String() }),
    async execute(_id, params, _signal, _onUpdate, ctx) { return { content: [{ type: "text", text: JSON.stringify(await executeCapability(ctx, "topic_activate", params)) }] }; },
  });
  pi.registerTool({
    name: "resource_read",
    label: "Read active resource",
    description: "Read the currently active document or a bounded region. This never opens or switches documents; use resource_manage operation=open first for a different note. The result is authoritative persisted content.",
    parameters: Type.Object({ target: Type.Optional(Type.String()), region: Type.Optional(Type.Record(Type.String(), Type.Unknown())) }),
    async execute(_id, params, _signal, _onUpdate, ctx) { return { content: [{ type: "text", text: JSON.stringify(await executeCapability(ctx, "resource_read", params)) }] }; },
  });
  pi.registerTool({
    name: "resource_mutate",
    label: "Modify active resource",
    description: "Edit the active document only. Use operation=append or operation=replace with a validated paragraph or exact-text region and expected_version when available. This is not for creating drafts or arbitrary files.",
    parameters: Type.Object({ operation: Type.Union([Type.Literal("append"), Type.Literal("replace")]), content: Type.String(), target: Type.Optional(Type.String()), region: Type.Optional(Type.Record(Type.String(), Type.Unknown())), expected_version: Type.Optional(Type.String()) }),
    async execute(_id, params, _signal, _onUpdate, ctx) { return { content: [{ type: "text", text: JSON.stringify(await executeCapability(ctx, "resource_mutate", params)) }] }; },
  });
  pi.registerTool({
    name: "resource_manage",
    label: "Manage resource",
    description: "Manage durable, promoted documents only. Every call requires operation. For open, use exactly {operation: \"open\", target: \"SAVED_RESOURCE\"}; target must be a saved resource name/path. Never pass an active unpromoted draft id here, and never use this tool to read a draft. Promote drafts with draft_manage first.",
    parameters: Type.Union([
      Type.Object({ operation: Type.Literal("active") }),
      Type.Object({ operation: Type.Literal("create"), target: Type.String(), content: Type.String() }),
      Type.Object({ operation: Type.Literal("open"), target: Type.String() }),
      Type.Object({ operation: Type.Literal("rename"), target: Type.String(), new_name: Type.String() }),
    ]),
    async execute(_id, params, _signal, _onUpdate, ctx) { return { content: [{ type: "text", text: JSON.stringify(await executeCapability(ctx, "resource_manage", params)) }] }; },
  });
  pi.registerTool({
    name: "draft_manage",
    label: "Manage composition draft",
    description: "Manage a temporary composition draft. EVERY call must include an operation field. CREATE ONLY OPENS A DRAFT and never saves, promotes, or implies approval. Canonical create call: {operation: \"create\", content: \"...\"}. Use the exact draft id returned by draft_manage; never abbreviate, reconstruct, or guess it. Read an active draft only with exactly {operation: \"read\", draft_id: \"EXACT_RETURNED_ID\"}; explicit draft-read requests must not be answered from context alone. revise requires the exact draft_id and complete replacement content; revision clears approval. Only after explicit approval in a current user turn may promote be called with {operation: \"promote\", draft_id: \"EXACT_RETURNED_ID\", target: \"document name\"}; target must be non-empty. Promotion creates the durable Rex Vault note and verifies it; still use resource_manage {operation: \"open\", target: \"SAVED_RESOURCE\"} then resource_read before claiming save completion.",
    // Keep one flat object rather than a oneOf/union. Gemma's native tool
    // serializer has emitted operation-shaped objects for this union instead
    // of the required string discriminator, which the backend correctly
    // rejects. Operation-specific requirements remain authoritative in Hermes.
    parameters: Type.Object({
      operation: Type.Union([Type.Literal("create"), Type.Literal("read"), Type.Literal("revise"), Type.Literal("discard"), Type.Literal("promote")]),
      draft_id: Type.Optional(Type.String()),
      content: Type.Optional(Type.String()),
      target: Type.Optional(Type.String()),
      source_handles: Type.Optional(Type.Array(Type.String())),
    }),
    async execute(_id, params, _signal, _onUpdate, ctx) { return { content: [{ type: "text", text: JSON.stringify(await executeCapability(ctx, "draft_manage", params)) }] }; },
  });
  pi.registerTool({
    name: "phone_hangup",
    label: "End phone call",
    description: "End the active Rex cellular call when the user explicitly asks to hang up or end the call. Do not use this for conversational sign-offs unless the user requests ending the phone call.",
    parameters: Type.Object({}),
    async execute(_id, params, _signal, _onUpdate, ctx) { return { content: [{ type: "text", text: JSON.stringify(await executeCapability(ctx, "phone_hangup", params)) }] }; },
  });
  pi.registerTool({
    name: "assignment_capture",
    label: "Capture assignment",
    description: "Persist explicit follow-up work for Hermes after this call. Use only when the user assigns a future task. If the user asks to be notified when it finishes, set notify_on_completion=true and notification_channel=\"sms\". The result is structured session state, not a document.",
    parameters: Type.Object({ request: Type.String(), topic: Type.Optional(Type.String()), assignment_type: Type.Optional(Type.Union([Type.Literal("writing"), Type.Literal("research"), Type.Literal("preparation")])), desired_outputs: Type.Optional(Type.Array(Type.String())), priority: Type.Optional(Type.Union([Type.Literal("normal"), Type.Literal("high")])), unresolved_questions: Type.Optional(Type.Array(Type.String())), source_handles: Type.Optional(Type.Array(Type.String())), transcript_refs: Type.Optional(Type.Array(Type.String())), notify_on_completion: Type.Optional(Type.Boolean()), notification_channel: Type.Optional(Type.Literal("sms")) }),
    async execute(_id, params, _signal, _onUpdate, ctx) { return { content: [{ type: "text", text: JSON.stringify(await executeCapability(ctx, "assignment_capture", params)) }] }; },
  });
}
