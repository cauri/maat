"""Event envelope + publish helpers, matching the Rust kernel's contract (maat-kerneld).

Subjects are `maat.events.<type>`; the JSON payload is the EventEnvelope the kernel decodes
and appends to the log.
"""

from __future__ import annotations

import json
from typing import Any

SUBJECT_PREFIX = "maat.events"


def envelope(stream_id: str, type_: str, data: dict[str, Any], tenant_id: str = "cauri") -> bytes:
    return json.dumps(
        {"stream_id": stream_id, "type": type_, "data": data, "tenant_id": tenant_id}
    ).encode()


async def publish(
    nc: Any, type_: str, stream_id: str, data: dict[str, Any], tenant_id: str = "cauri"
) -> None:
    await nc.publish(f"{SUBJECT_PREFIX}.{type_}", envelope(stream_id, type_, data, tenant_id))
