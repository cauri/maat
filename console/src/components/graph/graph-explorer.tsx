"use client";

import { useMemo } from "react";

import {
  Background,
  Controls,
  type Edge,
  MiniMap,
  type Node,
  Position,
  ReactFlow,
} from "@xyflow/react";
import { useTheme } from "next-themes";

import { Skeleton } from "@/components/ui/skeleton";
import { useGraph } from "@/hooks/use-graph";
import type { GraphResponse } from "@/lib/types";

import "@xyflow/react/dist/style.css";

function confidenceColor(c: number | undefined): string {
  if (c == null) return "#71717a";
  if (c >= 0.67) return "#10b981";
  if (c >= 0.34) return "#f59e0b";
  return "#f43f5e";
}

function buildFlow(data: GraphResponse | undefined): { nodes: Node[]; edges: Edge[] } {
  if (!data) return { nodes: [], edges: [] };
  const sources = data.nodes.filter((n) => n.type === "source");
  const clusters = data.nodes.filter((n) => n.type === "cluster");

  const nodes: Node[] = [
    ...sources.map((n, i) => ({
      id: n.id,
      position: { x: 0, y: i * 110 },
      data: { label: n.label },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      style: {
        width: 170,
        borderRadius: 8,
        border: "1px solid var(--border)",
        background: "var(--card)",
        color: "var(--card-foreground)",
        fontSize: 12,
        fontWeight: 500,
      },
    })),
    ...clusters.map((n, i) => ({
      id: n.id,
      position: { x: 460, y: i * 78 },
      data: {
        label:
          (n.label.length > 64 ? `${n.label.slice(0, 64)}…` : n.label) +
          (n.originators ? `  ·  ${n.originators}×` : ""),
      },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      style: {
        width: 260,
        borderRadius: 8,
        borderLeft: `4px solid ${confidenceColor(n.confidence)}`,
        border: "1px solid var(--border)",
        borderLeftWidth: 4,
        borderLeftColor: confidenceColor(n.confidence),
        background: "var(--card)",
        color: "var(--card-foreground)",
        fontSize: 11,
        padding: 8,
        textAlign: "left" as const,
      },
    })),
  ];

  const edges: Edge[] = data.edges.map((e, i) => ({
    id: `e${i}`,
    source: e.source,
    target: e.target,
    style: { stroke: "var(--muted-foreground)", strokeWidth: 1 },
  }));

  return { nodes, edges };
}

export function GraphExplorer() {
  const { data, isLoading } = useGraph();
  const { resolvedTheme } = useTheme();
  const { nodes, edges } = useMemo(() => buildFlow(data), [data]);

  if (isLoading) {
    return (
      <div className="p-4">
        <Skeleton className="h-[80vh] w-full" />
      </div>
    );
  }

  return (
    <div className="relative h-full w-full">
      <div className="pointer-events-none absolute left-3 top-3 z-10 flex flex-col gap-1 rounded-md border bg-background/80 p-2 text-xs backdrop-blur">
        <span className="font-medium">Corroboration graph</span>
        <span className="text-muted-foreground">sources → the facts they corroborate</span>
        <div className="mt-1 flex items-center gap-2">
          <span className="flex items-center gap-1">
            <span className="size-2 rounded-full" style={{ background: "#10b981" }} /> high
          </span>
          <span className="flex items-center gap-1">
            <span className="size-2 rounded-full" style={{ background: "#f59e0b" }} /> mid
          </span>
          <span className="flex items-center gap-1">
            <span className="size-2 rounded-full" style={{ background: "#f43f5e" }} /> low
          </span>
        </div>
      </div>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        colorMode={resolvedTheme === "light" ? "light" : "dark"}
        fitView
        minZoom={0.1}
        nodesDraggable
        nodesConnectable={false}
      >
        <Background />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable />
      </ReactFlow>
    </div>
  );
}
