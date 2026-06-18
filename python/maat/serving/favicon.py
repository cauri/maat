"""Outlet favicons for the Sources surfaces (#sources).

Fetched + cached SERVER-SIDE so neither the operator console nor the app loads third-party images
directly (privacy #1, same posture as the article-image proxy). DuckDuckGo's icon service, with a
deterministic lettered monogram fallback so a row is never a broken image. One implementation behind
both the console route (`/source-icon`) and the public app endpoint (`/api/v2/source-icon`).
"""

from __future__ import annotations

import html

_CACHE: dict[str, tuple[bytes, str]] = {}
_COLORS = ("#4263eb", "#1098ad", "#0ca678", "#e8590c", "#7048e8", "#c2255c", "#a8792e", "#3b5bdb")
_SVG = "image/svg+xml"


def valid_domain(d: str) -> bool:
    """A bare domain we can safely build the icon-service URL from — no schemes, paths, or oddities,
    so this can only ever fetch ``icons.duckduckgo.com/ip3/<domain>.ico`` (no SSRF surface)."""
    return bool(d) and len(d) <= 253 and "." in d and all(c.isalnum() or c in ".-" for c in d)


def monogram(domain: str) -> bytes:
    """A deterministic lettered chip for an outlet with no usable favicon — reads as intentional."""
    base = (domain or "").lower()
    if base.startswith("www."):
        base = base[4:]
    letter = html.escape((base[:1] or "?").upper())
    color = _COLORS[(sum(base.encode()) % len(_COLORS)) if base else 0]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<rect width="32" height="32" rx="7" fill="{color}"/>'
        f'<text x="16" y="23" text-anchor="middle" font-family="system-ui,-apple-system,sans-serif" '
        f'font-size="18" font-weight="600" fill="#fff">{letter}</text></svg>'
    ).encode()


async def icon_bytes(domain: str) -> tuple[bytes, str]:
    """``(bytes, content_type)`` for an outlet's domain — its real favicon if one resolves, else a
    monogram. Cached in-process per domain; the domain is validated first, so a miss or any fetch
    error degrades to the monogram rather than a broken image."""
    dom = (domain or "").strip().lower()
    if not valid_domain(dom):
        return monogram(dom), _SVG
    if dom in _CACHE:
        return _CACHE[dom]
    import httpx

    try:
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as http:
            r = await http.get(f"https://icons.duckduckgo.com/ip3/{dom}.ico")
        ctype = r.headers.get("content-type", "").split(";")[0].strip()
        # The service hands back a tiny blank placeholder for unknown domains — treat small or
        # non-image payloads as a miss and fall through to the monogram.
        if r.status_code == 200 and ctype.startswith("image/") and len(r.content) > 100:
            _CACHE[dom] = (r.content, ctype)
            return _CACHE[dom]
    except Exception:  # noqa: BLE001 — any fetch/parse error → monogram, never a broken image
        pass
    out = (monogram(dom), _SVG)
    _CACHE[dom] = out
    return out
