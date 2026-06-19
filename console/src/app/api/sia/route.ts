import { anthropic } from "@ai-sdk/anthropic";
import { convertToModelMessages, stepCountIs, streamText, type UIMessage } from "ai";

import { getSiaSystemPrompt, SIA_MODEL } from "@/lib/sia";
import { buildSiaTools } from "@/lib/sia-tools";

/**
 * Sia's chat endpoint (#306). Streams Claude (opus-4-8) with tool-calling over the command/query
 * API. Read tools run server-side; `propose_command` is resolved by the operator in the dock
 * (propose-and-confirm). The persona is loaded from the prompt registry (co-designed with cauri,
 * D29), so it's never fabricated here — if it can't be loaded, Sia refuses to run.
 */
export const maxDuration = 60;

export async function POST(req: Request): Promise<Response> {
  let payload: { messages?: UIMessage[]; room?: string; selection?: unknown };
  try {
    payload = await req.json();
  } catch {
    return new Response("Bad request", { status: 400 });
  }

  const cookie = req.headers.get("cookie") ?? "";
  const system = await getSiaSystemPrompt(payload.room ?? "", payload.selection, cookie);
  if (!system) {
    return new Response(
      "Sia is unavailable — her persona could not be loaded from the prompt registry.",
      { status: 503 },
    );
  }

  const result = streamText({
    model: anthropic(SIA_MODEL),
    system,
    messages: await convertToModelMessages(payload.messages ?? []),
    tools: buildSiaTools(cookie),
    stopWhen: stepCountIs(8),
  });

  return result.toUIMessageStreamResponse();
}
