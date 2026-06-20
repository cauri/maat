"use client";

import {
  type EdgeProps,
  getStraightPath,
  Handle,
  type InternalNode,
  type Node,
  type NodeProps,
  Position,
  useInternalNode,
} from "@xyflow/react";

import { cn } from "@/lib/utils";

export interface GraphNodeData {
  kind: "cluster" | "source";
  label: string;
  r: number;
  /** Tailwind classes for the circle fill/border. */
  tone: string;
  /** Independent-originator count, shown inside fact nodes. */
  count?: number;
  dim: boolean;
  focused: boolean;
  [key: string]: unknown;
}

const HIDDEN_HANDLE = "!size-0 !min-h-0 !min-w-0 !border-0 !bg-transparent";

/** A graph node as a sized, colour-coded circle. Labels show for sources + the focused node. */
export function GraphCircleNode({ data }: NodeProps) {
  const d = data as GraphNodeData;
  const size = d.r * 2;
  return (
    <div
      className={cn(
        "relative flex items-center justify-center rounded-full border-2 transition-opacity",
        d.tone,
        d.dim && "opacity-20",
        d.focused && "ring-2 ring-ring ring-offset-2 ring-offset-background",
      )}
      style={{ width: size, height: size }}
      title={d.label}
    >
      <Handle type="target" position={Position.Top} className={HIDDEN_HANDLE} style={{ left: "50%", top: "50%" }} />
      <Handle type="source" position={Position.Top} className={HIDDEN_HANDLE} style={{ left: "50%", top: "50%" }} />
      {d.kind === "cluster" && d.count != null && d.r >= 15 && (
        <span className="text-[10px] font-semibold tabular-nums text-white">{d.count}</span>
      )}
      {(d.focused || d.kind === "source") && !d.dim && (
        <span className="pointer-events-none absolute left-1/2 top-full mt-1 max-w-44 -translate-x-1/2 truncate text-center text-[10px] leading-tight text-foreground/80">
          {d.label}
        </span>
      )}
    </div>
  );
}

function geom(node: InternalNode<Node>) {
  const w = node.measured?.width ?? 0;
  const h = node.measured?.height ?? 0;
  return {
    x: (node.internals.positionAbsolute?.x ?? 0) + w / 2,
    y: (node.internals.positionAbsolute?.y ?? 0) + h / 2,
    r: w / 2,
  };
}

function boundary(from: { x: number; y: number; r: number }, to: { x: number; y: number }) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const len = Math.hypot(dx, dy) || 1;
  return { x: from.x + (dx / len) * from.r, y: from.y + (dy / len) * from.r };
}

/** A straight edge anchored at each node's circle boundary (center-to-center geometry). */
export function FloatingEdge({ id, source, target, markerEnd, style }: EdgeProps) {
  const s = useInternalNode(source);
  const t = useInternalNode(target);
  if (!s || !t) return null;
  const sc = geom(s);
  const tc = geom(t);
  const sp = boundary(sc, { x: tc.x, y: tc.y });
  const tp = boundary(tc, { x: sc.x, y: sc.y });
  const [path] = getStraightPath({ sourceX: sp.x, sourceY: sp.y, targetX: tp.x, targetY: tp.y });
  return <path id={id} className="react-flow__edge-path" d={path} style={style} markerEnd={markerEnd} />;
}
