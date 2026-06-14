"""The Claim — the atomic unit (BRIEF §5.1). Output schema of the Assessor."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Claim(BaseModel):
    """One atomic assertion extracted from an article.

    The reported layer of an attribution ("X said Y") is voice="own" — the outlet is
    accountable for getting the utterance right; the embedded claim ("Y") is
    voice="attributed" to its speaker. relay_chain records nested reporting
    (outlet -> relay -> original speaker).
    """

    text: str
    voice: Literal["own", "attributed"]
    speaker: str | None = None
    relay_chain: list[str] | None = None
    in_headline: bool = False
    evidence_span: str
