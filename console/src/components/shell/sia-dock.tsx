"use client";

import { CheckCircle2, Send, Sparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";

import { useShell } from "./shell-context";

/**
 * The Sia dock — placeholder for the operator's collaborator (#306).
 *
 * Deliberately just chrome: it marks where Sia lives on every page. Her runtime
 * persona/prompt is co-designed with cauri (D29) and wired in #306, so there is no
 * assistant voice, no system prompt, and no model wiring here — only an operator-facing
 * description of what she will do, and a disabled composer showing the seat she'll take.
 */
export function SiaDock() {
  const { sia } = useShell();

  return (
    <Sheet open={sia.open} onOpenChange={sia.set}>
      <SheetContent side="right" className="w-full gap-0 p-0 sm:max-w-md">
        <SheetHeader className="border-b">
          <div className="flex items-center gap-2">
            <span className="flex size-7 items-center justify-center rounded-md bg-primary/10 text-primary">
              <Sparkles className="size-4" />
            </span>
            <SheetTitle>Sia</SheetTitle>
            <Badge variant="secondary" className="ml-auto">
              Arriving in #306
            </Badge>
          </div>
          <SheetDescription>The operator&apos;s collaborator — on every page.</SheetDescription>
        </SheetHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto px-4 py-5 text-sm">
          <p className="text-muted-foreground">
            Sia will help you inspect, run, and correct what the engine produces — a teammate that
            co-owns the work, not a chat box.
          </p>
          <ul className="flex flex-col gap-2.5">
            {[
              "Acts through the same command API you do — no second backend.",
              "Every change is a typed, audited event.",
              "Proposes and shows the diff; the veracity core stays sign-off-gated.",
            ].map((line) => (
              <li key={line} className="flex items-start gap-2">
                <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-primary" />
                <span>{line}</span>
              </li>
            ))}
          </ul>
        </div>

        <SheetFooter className="border-t">
          <div className="flex flex-col gap-2">
            <Textarea
              disabled
              rows={2}
              placeholder="Sia's composer lands with #306…"
              aria-label="Message Sia (coming in #306)"
            />
            <Button disabled className="self-end">
              <Send /> Send
            </Button>
          </div>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
