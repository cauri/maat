"use client";

import { useState } from "react";

import { ExternalLink, Flag, Wand2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useClaim } from "@/hooks/use-claims";
import { useRunCommand } from "@/hooks/use-command";
import { ApiError } from "@/lib/api";
import type { ClaimDetail } from "@/lib/types";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-sm">{children}</span>
    </div>
  );
}

function ClaimBody({ detail }: { detail: ClaimDetail }) {
  const run = useRunCommand([["claims"], ["claim", detail.id]]);
  const [kind, setKind] = useState(detail.kind ?? "");
  const [voice, setVoice] = useState(detail.voice ?? "");
  const [speaker, setSpeaker] = useState(detail.speaker ?? "");
  const [abuse, setAbuse] = useState("");

  const correct = () => {
    const body: Record<string, unknown> = { claim_id: detail.id, reason: "operator correction" };
    if (kind.trim() && kind !== detail.kind) body.kind = kind.trim();
    if (voice.trim() && voice !== detail.voice) body.voice = voice.trim();
    if (speaker.trim() && speaker !== (detail.speaker ?? "")) body.speaker = speaker.trim();
    if (!("kind" in body) && !("voice" in body) && !("speaker" in body)) return;
    run.mutate({ name: "claim.correct", body });
  };

  const flag = () => {
    const a = abuse.trim();
    if (!a) return;
    run.mutate(
      { name: "claim.flag_laundering", body: { claim_id: detail.id, abuse: a, reason: "operator flag" } },
      { onSuccess: () => setAbuse("") },
    );
  };

  return (
    <div className="flex flex-col gap-6 p-4">
      <p className="text-sm leading-relaxed">{detail.text}</p>

      <section className="grid grid-cols-2 gap-4 border-t pt-4">
        <Field label="Source">
          {detail.url ? (
            <a
              href={detail.url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 underline-offset-4 hover:underline"
            >
              {detail.source} <ExternalLink className="size-3" />
            </a>
          ) : (
            detail.source
          )}
        </Field>
        <Field label="Language">{detail.language ?? "—"}</Field>
        <Field label="In headline">{detail.in_headline ? "Yes" : "No"}</Field>
        <Field label="Claim id">
          <code className="font-mono text-xs">{detail.id}</code>
        </Field>
      </section>

      {detail.cluster && (
        <section className="flex flex-col gap-2 border-t pt-4">
          <h3 className="text-sm font-medium">Cluster</h3>
          <p className="text-sm text-muted-foreground">{detail.cluster.fact}</p>
          <div className="flex flex-wrap gap-2 text-xs">
            <Badge variant="secondary">confidence {detail.cluster.confidence.toFixed(2)}</Badge>
            <Badge variant="secondary" className="capitalize">
              {detail.cluster.extremity}
            </Badge>
            <Badge variant="secondary">
              {detail.cluster.independent_originators} independent
            </Badge>
          </div>
        </section>
      )}

      <section className="flex flex-col gap-3 border-t pt-4">
        <h3 className="text-sm font-medium">Correct classification</h3>
        <p className="text-xs text-muted-foreground">
          An audited <code>admin.classification.corrected</code> event — the kernel marks the claim
          corrected so a re-run won&apos;t clobber it (F3/D5).
        </p>
        <div className="grid grid-cols-3 gap-2">
          <LabeledInput id="kind" label="Kind" value={kind} onChange={setKind} />
          <LabeledInput id="voice" label="Voice" value={voice} onChange={setVoice} />
          <LabeledInput id="speaker" label="Speaker" value={speaker} onChange={setSpeaker} />
        </div>
        <Button size="sm" className="self-start" onClick={correct} disabled={run.isPending}>
          <Wand2 /> Apply correction
        </Button>
      </section>

      <section className="flex flex-col gap-2 border-t pt-4">
        <h3 className="text-sm font-medium">Flag laundering</h3>
        <p className="text-xs text-muted-foreground">
          Mark §5.2 abuse the classifier missed (an extraordinary claim relayed as fact).
        </p>
        <div className="flex items-center gap-2">
          <Input
            value={abuse}
            onChange={(e) => setAbuse(e.target.value)}
            placeholder="What's being laundered?"
            className="h-8"
            onKeyDown={(e) => e.key === "Enter" && flag()}
          />
          <Button variant="destructive" size="sm" onClick={flag} disabled={run.isPending || !abuse.trim()}>
            <Flag /> Flag
          </Button>
        </div>
      </section>
    </div>
  );
}

function LabeledInput({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs text-muted-foreground">
        {label}
      </label>
      <Input id={id} value={value} onChange={(e) => onChange(e.target.value)} className="h-8" />
    </div>
  );
}

export function ClaimWorkspace({ claimId, onClose }: { claimId: string | null; onClose: () => void }) {
  const { data, isLoading, error } = useClaim(claimId);

  return (
    <Sheet open={claimId != null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto p-0 sm:max-w-lg">
        <SheetHeader className="border-b">
          <SheetTitle className="text-base">Claim</SheetTitle>
          <SheetDescription>Full provenance — inspect and correct (operator-only).</SheetDescription>
        </SheetHeader>
        {isLoading ? (
          <div className="flex flex-col gap-3 p-4">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : error ? (
          <p className="p-4 text-sm text-destructive">
            {error instanceof ApiError ? error.message : "Failed to load claim"}
          </p>
        ) : data ? (
          <ClaimBody key={data.id} detail={data} />
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
