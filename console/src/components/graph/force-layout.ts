import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationNodeDatum,
} from "d3-force";

import type { GraphEdge, GraphNode } from "@/lib/types";

interface SimNode extends SimulationNodeDatum {
  id: string;
  r: number;
}

interface SimLink {
  source: string;
  target: string;
}

export interface Positioned {
  x: number;
  y: number;
}

/**
 * Settle a force-directed layout synchronously and deterministically. Initial positions are seeded
 * in a ring by index (no RNG), so the same graph always lays out the same way — corroborated facts
 * pull toward their shared sources, isolated ones drift to the edge. Runs a fixed number of ticks
 * (no animation) so it can live in a render-time useMemo.
 */
export function computeLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  radiusOf: (node: GraphNode) => number,
): Map<string, Positioned> {
  const simNodes: SimNode[] = nodes.map((n, i) => {
    const angle = (i / Math.max(1, nodes.length)) * 2 * Math.PI;
    return { id: n.id, r: radiusOf(n), x: Math.cos(angle) * 320, y: Math.sin(angle) * 320 };
  });
  const ids = new Set(simNodes.map((n) => n.id));
  const simLinks: SimLink[] = edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }));

  const sim = forceSimulation<SimNode>(simNodes)
    .force("charge", forceManyBody<SimNode>().strength(-420))
    .force(
      "link",
      forceLink<SimNode, SimLink>(simLinks)
        .id((d) => d.id)
        .distance(130)
        .strength(0.5),
    )
    .force("center", forceCenter(0, 0))
    .force("collide", forceCollide<SimNode>().radius((d) => d.r + 14))
    .stop();

  sim.tick(320);

  const out = new Map<string, Positioned>();
  for (const n of simNodes) out.set(n.id, { x: n.x ?? 0, y: n.y ?? 0 });
  return out;
}
