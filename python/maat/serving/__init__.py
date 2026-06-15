"""Serving layer (P5) — assemble per-tenant views over the shared veracity projections.

The veracity core (corroboration, reputation, calibration) is computed once and shared; this
package layers the per-user/per-tenant concerns on top (feed assembly, topics, tenancy isolation,
auth, social) without touching the kernel — reads projections, stores user state as events.
"""
