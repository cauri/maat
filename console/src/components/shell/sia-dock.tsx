"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { usePathname } from "next/navigation";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { Check, CircleSlash, Loader2, Send, ShieldAlert, Sparkles, Square, Wrench } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, runCommand } from "@/lib/api";
import { roomForPath } from "@/lib/rooms";
import { cn } from "@/lib/utils";

import { useShell } from "./shell-context";

/** Commands that change the veracity core — flagged for sign-off (mirrors the #304 manifest). */
const SIGNOFF = new Set(["config.promote", "prompt.update"]);

interface ProposalInput {
  command: string;
  args: Record<string, unknown>;
  rationale: string;
}

interface ToolPart {
  type: string;
  toolCallId: string;
  state: "input-streaming" | "input-available" | "output-available" | "output-error";
  input?: unknown;
  output?: unknown;
  errorText?: string;
}

const SUGGESTIONS = [
  "What needs my attention right now?",
  "Which sources look least reliable?",
  "Summarise pipeline health.",
];

export function SiaDock() {
  const { sia, selection } = useShell();
  const pathname = usePathname();
  const room = roomForPath(pathname);
  const [input, setInput] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  const transport = useMemo(() => new DefaultChatTransport({ api: "/api/sia" }), []);
  const { messages, sendMessage, addToolResult, status, stop, error } = useChat({ transport });

  const busy = status === "submitted" || status === "streaming";

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const send = (text: string) => {
    const body = {
      room: room?.title ?? "Console",
      selection: selection ?? undefined,
    };
    sendMessage({ text }, { body });
  };

  const submit = () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    send(text);
  };

  const resolveProposal = async (part: ToolPart) => {
    const proposal = part.input as ProposalInput;
    try {
      const result = await runCommand(proposal.command, {
        ...proposal.args,
        reason: proposal.rationale,
      });
      toast.success("Change applied", { description: `${result.command} — ${result.event_type}` });
      addToolResult({
        tool: "propose_command",
        toolCallId: part.toolCallId,
        output: { confirmed: true, result },
      });
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Command failed";
      toast.error("Couldn't apply the change", { description: message });
      addToolResult({
        tool: "propose_command",
        toolCallId: part.toolCallId,
        output: { confirmed: false, error: message },
      });
    }
  };

  const dismissProposal = (part: ToolPart) =>
    addToolResult({
      tool: "propose_command",
      toolCallId: part.toolCallId,
      output: { confirmed: false, declined: true },
    });

  return (
    <Sheet open={sia.open} onOpenChange={sia.set}>
      <SheetContent side="right" className="w-full gap-0 p-0 sm:max-w-md">
        <SheetHeader className="border-b">
          <div className="flex items-center gap-2">
            <span className="flex size-7 items-center justify-center rounded-md bg-primary/10 text-primary">
              <Sparkles className="size-4" />
            </span>
            <SheetTitle>Sia</SheetTitle>
            <Badge variant="secondary" className="ml-auto font-normal">
              {room?.title ?? "Console"}
            </Badge>
          </div>
          <SheetDescription>
            Your collaborator — reads the live data, proposes audited changes for your sign-off.
          </SheetDescription>
        </SheetHeader>

        <ScrollArea className="min-h-0 flex-1">
          <div className="flex flex-col gap-4 px-4 py-4">
            {messages.length === 0 && (
              <div className="flex flex-col gap-3 pt-6">
                <p className="text-sm text-muted-foreground">
                  Ask Sia about what you&apos;re looking at, or have her stage a correction.
                </p>
                <div className="flex flex-col items-start gap-1.5">
                  {SUGGESTIONS.map((s) => (
                    <button
                      key={s}
                      type="button"
                      onClick={() => send(s)}
                      className="rounded-md border px-2.5 py-1.5 text-left text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((message) => (
              <div key={message.id} className="flex flex-col gap-2">
                {message.parts.map((part, i) => {
                  if (part.type === "text") {
                    return (
                      <div
                        key={i}
                        className={cn(
                          "whitespace-pre-wrap text-sm leading-relaxed",
                          message.role === "user"
                            ? "self-end rounded-lg bg-primary px-3 py-2 text-primary-foreground"
                            : "text-foreground",
                        )}
                      >
                        {part.text}
                      </div>
                    );
                  }
                  if (part.type === "tool-propose_command") {
                    return <ProposalCard key={i} part={part as ToolPart} onConfirm={resolveProposal} onDismiss={dismissProposal} />;
                  }
                  if (part.type.startsWith("tool-")) {
                    return <ReadToolChip key={i} part={part as ToolPart} />;
                  }
                  return null;
                })}
              </div>
            ))}

            {status === "submitted" && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" /> Sia is thinking…
              </div>
            )}
            {error && (
              <p className="text-sm text-destructive">
                Sia hit an error. {error.message}
              </p>
            )}
            <div ref={endRef} />
          </div>
        </ScrollArea>

        <div className="border-t p-3">
          <div className="flex items-end gap-2">
            <Textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  submit();
                }
              }}
              placeholder="Message Sia…  (Enter to send)"
              rows={2}
              className="min-h-0 resize-none"
            />
            {busy ? (
              <Button size="icon" variant="outline" onClick={() => stop()} aria-label="Stop">
                <Square />
              </Button>
            ) : (
              <Button size="icon" onClick={submit} disabled={!input.trim()} aria-label="Send">
                <Send />
              </Button>
            )}
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function ReadToolChip({ part }: { part: ToolPart }) {
  const name = part.type.replace(/^tool-/, "").replace(/_/g, " ");
  const done = part.state === "output-available";
  return (
    <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
      {done ? <Check className="size-3 text-emerald-500" /> : <Loader2 className="size-3 animate-spin" />}
      {done ? "Looked at" : "Reading"} {name}
    </div>
  );
}

function ProposalCard({
  part,
  onConfirm,
  onDismiss,
}: {
  part: ToolPart;
  onConfirm: (part: ToolPart) => void | Promise<void>;
  onDismiss: (part: ToolPart) => void;
}) {
  const [busy, setBusy] = useState(false);

  if (part.state === "input-streaming") {
    return (
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Wrench className="size-3 animate-pulse" /> Sia is preparing a change…
      </div>
    );
  }

  const proposal = part.input as ProposalInput | undefined;
  if (!proposal) return null;
  const signoff = SIGNOFF.has(proposal.command);

  // Already resolved (confirmed / declined): show the outcome.
  if (part.state === "output-available" || part.state === "output-error") {
    const out = part.output as { confirmed?: boolean; declined?: boolean; error?: string } | undefined;
    return (
      <div className="rounded-lg border px-3 py-2 text-xs">
        <code className="font-mono">{proposal.command}</code>{" "}
        {out?.confirmed ? (
          <span className="text-emerald-600 dark:text-emerald-400">— applied ✓</span>
        ) : out?.declined ? (
          <span className="text-muted-foreground">— dismissed</span>
        ) : (
          <span className="text-destructive">— failed{out?.error ? `: ${out.error}` : ""}</span>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-primary/40 bg-primary/5 p-3">
      <div className="flex items-center gap-2">
        <Wrench className="size-3.5 text-primary" />
        <span className="text-sm font-medium">Sia proposes a change</span>
        {signoff && (
          <Badge variant="secondary" className="ml-auto gap-1 text-amber-600 dark:text-amber-400">
            <ShieldAlert className="size-3" /> sign-off
          </Badge>
        )}
      </div>
      <code className="block rounded bg-background/60 px-2 py-1 font-mono text-xs">
        {proposal.command}({JSON.stringify(proposal.args)})
      </code>
      <p className="text-sm text-muted-foreground">{proposal.rationale}</p>
      <div className="flex items-center justify-end gap-2 pt-0.5">
        <Button variant="ghost" size="sm" onClick={() => onDismiss(part)} disabled={busy}>
          <CircleSlash /> Dismiss
        </Button>
        <Button
          size="sm"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            await onConfirm(part);
            setBusy(false);
          }}
        >
          {busy ? <Loader2 className="animate-spin" /> : <Check />}
          {signoff ? "Sign off & apply" : "Apply"}
        </Button>
      </div>
    </div>
  );
}
