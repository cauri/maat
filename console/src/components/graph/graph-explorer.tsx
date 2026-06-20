"use client";

import { useMemo, useState } from "react";

import {
  Background,
  Controls,
  type Edge,
  MiniMap,
  type Node,
  ReactFlow,
} from "@xyflow/react";
import { Network, X } from "lucide-react";
import { useTheme } from "next-themes";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useGraph } from "@/hooks/use-graph";
import type { GraphNode } from "@/lib/types";
import { cn } from "@/lib/utils";

import { computeLayout } from "./force-layout";
import { FloatingEdge, type GraphNodeData, GraphCircleNode } from "./graph-parts";

import "@xyflow/react/dist/style.css";

const nodeTypes = { circle: GraphCircleNode };
const edgeTypes = { floating: FloatingEdge };

function clusterTone(confidence?: number): string {
  const c = confidence ?? 0;
  if (c >= 0.67) return "bg-emerald-500 border-emerald-600";
  if (c >= 0.34) return "bg-amber-500 border-amber-600";
  return "bg-rose-500 border-rose-600";
}
function toneOf(n: GraphNode): string {
  return n.type === "cluster" ? clusterTone(n.confidence) : "bg-muted border-border";
}

export function GraphExplorer() {
  const { data, isLoading } = useGraph();
  const { resolvedTheme } = useTheme();
  const [focus, setFocus] = useState<string | null>(null);

  const { layout, rById } = useMemo(() => {
    const nodes = data?.nodes ?? [];
    const edges = data?.edges ?? [];
    const deg = new Map<string, number>();
    for (const e of edges) {
      deg.set(e.source, (deg.get(e.source) ?? 0) + 1);
      deg.set(e.target, (deg.get(e.target) ?? 0) + 1);
    }
    const radiusOf = (n: GraphNode) =>
      n.type === "cluster"
        ? 14 + Math.min(n.originators ?? 1, 6) * 4
        : 11 + Math.min(deg.get(n.id) ?? 1, 10) * 1.6;
    return {
      layout: computeLayout(nodes, edges, radiusOf),
      rById: new Map(nodes.map((n) => [n.id, radiusOf(n)])),
    };
  }, [data]);

  const neighbors = useMemo(() => {
    if (!focus || !data) return null;
    const set = new Set<string>([focus]);
    for (const e of data.edges) {
      if (e.source === focus) set.add(e.target);
      if (e.target === focus) set.add(e.source);
    }
    return set;
  }, [focus, data]);

  const rfNodes = useMemo<Node<GraphNodeData>[]>(() => {
    return (data?.nodes ?? []).map((n) => {
      const pos = layout.get(n.id) ?? { x: 0, y: 0 };
      const r = rById.get(n.id) ?? 14;
      return {
        id: n.id,
        type: "circle",
        position: { x: pos.x - r, y: pos.y - r },
        data: {
          kind: n.type,
          label: n.label,
          r,
          tone: toneOf(n),
          count: n.type === "cluster" ? n.originators : undefined,
          dim: neighbors != null && !neighbors.has(n.id),
          focused: focus === n.id,
        },
        draggable: false,
      };
    });
  }, [data, layout, rById, neighbors, focus]);

  const rfEdges = useMemo<Edge[]>(() => {
    return (data?.edges ?? []).map((e, i) => {
      const incident = focus != null && (e.source === focus || e.target === focus);
      const faded = focus != null && !incident;
      return {
        id: `e${i}`,
        source: e.source,
        target: e.target,
        type: "floating",
        style: {
          stroke: incident ? "var(--primary)" : "var(--muted-foreground)",
          strokeWidth: incident ? 1.6 : 1,
          opacity: faded ? 0.08 : incident ? 0.9 : 0.35,
        },
      };
    });
  }, [data, focus]);

  if (isLoading) {
    return (
      <div className="p-4">
        <Skeleton className="h-[80vh] w-full" />
      </div>
    );
  }

  const focusNode = data?.nodes.find((n) => n.id === focus) ?? null;

  return (
    <div className="relative h-full w-full">
      <div className="pointer-events-none absolute left-3 top-3 z-10 flex flex-col gap-1 rounded-md border bg-background/85 p-2 text-xs backdrop-blur">
        <span className="flex items-center gap-1.5 font-medium">
          <Network className="size-3.5" /> Corroboration graph
        </span>
        <span className="text-muted-foreground">
          facts sized by independent sources · click a node to focus
        </span>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
          <Legend className="bg-emerald-500" label="well corroborated" />
          <Legend className="bg-amber-500" label="developing" />
          <Legend className="bg-rose-500" label="thin" />
          <Legend className="bg-muted border" label="source" />
        </div>
      </div>

      {focusNode && (
        <FocusPanel node={focusNode} graph={data} onClose={() => setFocus(null)} onSelect={setFocus} />
      )}

      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        colorMode={resolvedTheme === "light" ? "light" : "dark"}
        onNodeClick={(_, node) => setFocus(node.id)}
        onPaneClick={() => setFocus(null)}
        fitView
        minZoom={0.1}
        nodesDraggable={false}
        nodesConnectable={false}
      >
        <Background />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable nodeColor={() => "var(--muted-foreground)"} />
      </ReactFlow>
    </div>
  );
}

function Legend({ className, label }: { className: string; label: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className={cn("size-2 rounded-full", className)} /> {label}
    </span>
  );
}

function FocusPanel({
  node,
  graph,
  onClose,
  onSelect,
}: {
  node: GraphNode;
  graph: ReturnType<typeof useGraph>["data"];
  onClose: () => void;
  onSelect: (id: string) => void;
}) {
  const edges = graph?.edges ?? [];
  const byId = new Map((graph?.nodes ?? []).map((n) => [n.id, n]));
  // For a fact: the sources backing it. For a source: the facts it corroborates.
  const links =
    node.type === "cluster"
      ? edges.filter((e) => e.target === node.id).map((e) => byId.get(e.source))
      : edges.filter((e) => e.source === node.id).map((e) => byId.get(e.target));
  const related = links.filter((n): n is GraphNode => n != null);

  return (
    <div className="absolute right-3 top-3 z-10 flex max-h-[calc(100%-1.5rem)] w-72 flex-col gap-3 overflow-auto rounded-lg border bg-card/95 p-3 text-sm shadow-lg backdrop-blur">
      <div className="flex items-start justify-between gap-2">
        <Badge variant="secondary" className="capitalize">
          {node.type === "cluster" ? "fact" : "source"}
        </Badge>
        <button type="button" onClick={onClose} aria-label="Close" className="text-muted-foreground hover:text-foreground">
          <X className="size-4" />
        </button>
      </div>

      <p className="font-medium leading-snug">{node.label}</p>

      {node.type === "cluster" && (
        <div className="flex flex-wrap gap-1.5 text-xs">
          {node.confidence != null && <Badge variant="secondary">confidence {node.confidence.toFixed(2)}</Badge>}
          {node.extremity && <Badge variant="secondary" className="capitalize">{node.extremity}</Badge>}
          <Badge variant="secondary">{node.originators ?? 0} independent</Badge>
        </div>
      )}

      <div className="flex flex-col gap-1.5 border-t pt-2">
        <span className="text-xs font-medium text-muted-foreground">
          {node.type === "cluster"
            ? `Corroborated by ${related.length} source${related.length === 1 ? "" : "s"}`
            : `Corroborates ${related.length} fact${related.length === 1 ? "" : "s"}`}
        </span>
        {related.map((r) => (
          <button
            key={r.id}
            type="button"
            onClick={() => onSelect(r.id)}
            className="truncate rounded px-1.5 py-1 text-left text-xs hover:bg-muted"
            title={r.label}
          >
            {r.label}
          </button>
        ))}
      </div>
    </div>
  );
}
