export type ActionTool = "retrieval" | "draft_manage" | "assignment_capture" | "phone_hangup";

export const ALL_REX_TOOLS = [
  "retrieval", "topic_activate", "resource_read", "resource_mutate", "resource_manage",
  "draft_manage", "assignment_capture", "phone_hangup",
];

export function explicitHangupRequest(text: string): boolean {
  if (/\b(?:do\s+not|don't|never|not)\s+(?:please\s+)?hang\s+up\b/i.test(text)) return false;
  return /\b(?:end|terminate|disconnect|stop)\b(?:\s+(?:the|this|our))?\s+(?:phone\s+)?call\b[!?.,]*$/i.test(text.trim())
    || /\bhang\s*up\b(?:\s+(?:now|please))?\s*[!?.,]*$/i.test(text.trim());
}

// Phone turns carry bounded background state in the same user message as the
// live request. Routing must never let historical state choose the current
// action (for example, an old assignment must not hijack a memory question).
export function liveRequestText(text: string): string {
  const marker = "[Rex Voice live user request";
  const start = text.lastIndexOf(marker);
  if (start < 0) return text;
  const end = text.indexOf("]", start);
  if (end < 0) return text.slice(start + marker.length).trim();
  return text.slice(end + 1).trim();
}

export function requestedAction(text: string): ActionTool | undefined {
  text = liveRequestText(text);
  if (explicitHangupRequest(text)) return "phone_hangup";
  if (/\bassignment\b/i.test(text) || /\bassign(?:ed|s|ment)?\b/i.test(text)) {
    return "assignment_capture";
  }
  if (/\b(?:remember|recall|memory|memories|notes?|what did we decide)\b/i.test(text)) {
    return "retrieval";
  }
  const hasDocumentTarget = /\b(document|draft|note|file|saved|suggestions\.md)\b/i.test(text);
  if (hasDocumentTarget && /\b(create|write|revise|edit|change|append|replace|promote|save)\b/i.test(text)) {
    return "draft_manage";
  }
  return undefined;
}

export function actionTools(text: string): string[] {
  text = liveRequestText(text);
  const requested = requestedAction(text);
  if (requested === "phone_hangup") return ["phone_hangup"];
  if (requested === "assignment_capture") return ["assignment_capture"];
  const hasDocumentTarget = /\b(document|draft|note|file|saved|suggestions\.md)\b/i.test(text);
  if (!hasDocumentTarget) return ALL_REX_TOOLS;
  if (/\b(open|read)\b/i.test(text) && !/\b(create|write|revise|edit|change|append|replace|promote|save)\b/i.test(text)) {
    return ["draft_manage", "resource_manage", "resource_read"];
  }
  if (/\b(create|write|revise|edit|change|append|replace|promote|save)\b/i.test(text)) {
    return ["draft_manage"];
  }
  return ALL_REX_TOOLS;
}

export function routeTurn(text: string, assignmentCapturePending: boolean): {
  activeTools: string[];
  requested?: ActionTool;
} {
  const requested = requestedAction(text);
  if (assignmentCapturePending && requested === undefined) {
    return { activeTools: ["assignment_capture"], requested: "assignment_capture" };
  }
  return {
    activeTools: requested === undefined
      ? actionTools(text).filter((tool) => tool !== "phone_hangup")
      : actionTools(text),
    requested,
  };
}
