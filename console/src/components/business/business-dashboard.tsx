"use client";

import { Banknote, Eye, FlaskConical, Mail, MousePointerClick, UserPlus } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useAcquisition, useSpend } from "@/hooks/use-business";

function usd(n: number): string {
  return `$${n.toFixed(n !== 0 && Math.abs(n) < 1 ? 4 : 2)}`;
}

export function BusinessDashboard() {
  const spend = useSpend();
  const acq = useAcquisition();

  const funnelCards = [
    { icon: Eye, label: "Page views", value: acq.data?.funnel.views },
    { icon: MousePointerClick, label: "Store clicks", value: acq.data?.funnel.clicks },
    { icon: Mail, label: "Notify requests", value: acq.data?.funnel.notifies },
    { icon: UserPlus, label: "Signups", value: acq.data?.funnel.signups },
    { icon: FlaskConical, label: "Beta", value: acq.data?.funnel.beta },
  ];

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 p-4 sm:p-6">
      {/* Spend */}
      <section className="flex flex-col gap-4">
        <div className="flex items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
            <Banknote className="size-5" /> Spend
          </h2>
          {spend.data && (
            <span className="text-sm text-muted-foreground">
              LLM total <span className="font-semibold text-foreground tabular-nums">{usd(spend.data.llm.total_usd)}</span>
            </span>
          )}
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">LLM cost by stage</CardTitle>
            </CardHeader>
            <CardContent className="divide-y py-0">
              {spend.isLoading ? (
                <Skeleton className="my-3 h-24 w-full" />
              ) : (
                spend.data?.llm.stages.map((s) => (
                  <div key={s.stage} className="flex items-center justify-between gap-3 py-2.5">
                    <div className="flex flex-col">
                      <span className="text-sm font-medium capitalize">{s.stage}</span>
                      <span className="font-mono text-xs text-muted-foreground">{s.model}</span>
                    </div>
                    <div className="flex flex-col items-end">
                      <span className="text-sm font-medium tabular-nums">{usd(s.usd)}</span>
                      <span className="text-xs text-muted-foreground tabular-nums">
                        {s.calls.toLocaleString()} calls
                      </span>
                    </div>
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Acquisition cost by provider</CardTitle>
            </CardHeader>
            <CardContent className="divide-y py-0">
              {spend.isLoading ? (
                <Skeleton className="my-3 h-24 w-full" />
              ) : spend.data && spend.data.providers.length > 0 ? (
                spend.data.providers.map((p) => (
                  <div key={p.provider} className="flex items-center justify-between gap-3 py-2.5">
                    <div className="flex flex-col">
                      <span className="text-sm font-medium">{p.provider}</span>
                      <span className="text-xs text-muted-foreground tabular-nums">
                        {p.articles.toLocaleString()} articles
                      </span>
                    </div>
                    <span className="text-sm font-medium tabular-nums">{usd(p.usd)}</span>
                  </div>
                ))
              ) : (
                <p className="py-4 text-sm text-muted-foreground">No provider spend recorded.</p>
              )}
            </CardContent>
          </Card>
        </div>
      </section>

      {/* Acquisition */}
      <section className="flex flex-col gap-4">
        <h2 className="text-lg font-semibold tracking-tight">Acquisition funnel</h2>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
          {funnelCards.map(({ icon: Icon, label, value }) => (
            <Card key={label}>
              <CardContent className="flex flex-col gap-1 py-4">
                <span className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  <Icon className="size-3.5" /> {label}
                </span>
                {acq.isLoading ? (
                  <Skeleton className="h-8 w-12" />
                ) : (
                  <span className="text-2xl font-semibold tabular-nums">{(value ?? 0).toLocaleString()}</span>
                )}
              </CardContent>
            </Card>
          ))}
        </div>

        {acq.data && acq.data.by_platform.length > 0 && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Store clicks by platform</CardTitle>
            </CardHeader>
            <CardContent className="divide-y py-0">
              {acq.data.by_platform.map((p) => (
                <div key={p.platform} className="flex items-center justify-between gap-3 py-2.5 text-sm">
                  <span className="capitalize">{p.platform}</span>
                  <span className="tabular-nums text-muted-foreground">{p.clicks.toLocaleString()}</span>
                </div>
              ))}
            </CardContent>
          </Card>
        )}
      </section>
    </div>
  );
}
