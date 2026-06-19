/**
 * The room registry — the single source of truth for the console's navigation.
 *
 * Rooms are grouped by the two **altitudes** that organise the whole app (D33,
 * `console/README.md`):
 *   • Product mirror — verify what users see (Stories, Sources)
 *   • Engine room    — inspect / run / shape what produces it (Claims, Graph, Pipeline, Tuning)
 * …with Overview on top and the Inputs (Feedback, Business) below. The Audit drawer
 * and Sia are cross-cutting (everywhere), so they are not rooms — they live in the shell.
 */

import {
  Activity,
  LayoutDashboard,
  ListChecks,
  type LucideIcon,
  MessagesSquare,
  Network,
  Newspaper,
  ShieldCheck,
  SlidersHorizontal,
  Wallet,
} from "lucide-react";

export type Altitude = "overview" | "product" | "engine" | "inputs";

export interface Room {
  /** Stable id, also the URL segment. */
  id: string;
  /** Display name in the rail / palette / header. */
  title: string;
  /** Route path. */
  path: string;
  icon: LucideIcon;
  altitude: Altitude;
  /** Tracking issue (sub-issue of the #302 epic). */
  issue: number;
  /** One-line role, shown under the title and in the palette. */
  blurb: string;
}

export const ROOMS: Room[] = [
  {
    id: "overview",
    title: "Overview",
    path: "/overview",
    icon: LayoutDashboard,
    altitude: "overview",
    issue: 307,
    blurb: "KPIs, what needs you, trends, and the de-US readout.",
  },
  {
    id: "stories",
    title: "Stories",
    path: "/stories",
    icon: Newspaper,
    altitude: "product",
    issue: 308,
    blurb: "Credibility — the reader view, the why, and inline correction.",
  },
  {
    id: "sources",
    title: "Sources",
    path: "/sources",
    icon: ShieldCheck,
    altitude: "product",
    issue: 309,
    blurb: "Reliability — one canonical tier and trajectory per outlet.",
  },
  {
    id: "claims",
    title: "Claims",
    path: "/claims",
    icon: ListChecks,
    altitude: "engine",
    issue: 310,
    blurb: "The claim inspector — article→claim provenance. Not the reader feed.",
  },
  {
    id: "graph",
    title: "Graph",
    path: "/graph",
    icon: Network,
    altitude: "engine",
    issue: 315,
    blurb: "The corroboration-graph explorer.",
  },
  {
    id: "pipeline",
    title: "Pipeline",
    path: "/pipeline",
    icon: Activity,
    altitude: "engine",
    issue: 311,
    blurb: "Health & ops — activity, quality, calibration, updates.",
  },
  {
    id: "tuning",
    title: "Tuning",
    path: "/tuning",
    icon: SlidersHorizontal,
    altitude: "engine",
    issue: 312,
    blurb: "Prompts, config, and policy — sign-off gated.",
  },
  {
    id: "feedback",
    title: "Feedback",
    path: "/feedback",
    icon: MessagesSquare,
    altitude: "inputs",
    issue: 313,
    blurb: "Triage and coordinated-attack detection.",
  },
  {
    id: "business",
    title: "Business",
    path: "/business",
    icon: Wallet,
    altitude: "inputs",
    issue: 314,
    blurb: "Spend and acquisition.",
  },
];

/** Altitude groups, in rail order. A `null` label renders the group without a heading. */
export const ALTITUDE_GROUPS: { id: Altitude; label: string | null }[] = [
  { id: "overview", label: null },
  { id: "product", label: "Product mirror" },
  { id: "engine", label: "Engine room" },
  { id: "inputs", label: "Inputs" },
];

export function roomsByAltitude(altitude: Altitude): Room[] {
  return ROOMS.filter((room) => room.altitude === altitude);
}

/** The room owning a given pathname (longest matching prefix), or undefined. */
export function roomForPath(pathname: string): Room | undefined {
  return ROOMS.filter((room) => pathname === room.path || pathname.startsWith(`${room.path}/`)).sort(
    (a, b) => b.path.length - a.path.length,
  )[0];
}
