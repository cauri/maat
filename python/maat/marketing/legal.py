"""Legal pages for the public marketing site — Privacy Policy + Legal Notice (Imprint).

GDPR-oriented and matched to what this site actually does: it is cookieless, has no third-party
trackers, stores no IP addresses in its analytics, and collects an email only when a visitor asks
to be told at launch (plus an explicit beta opt-in). The policy is written to that reality.

DRAFT pending cauri's review. The data practices are accurate to the code, but the formal
controller identity (legal name + postal address) is a placeholder — fill CONTROLLER / POSTAL
below before relying on these pages legally, and have counsel glance at them. Everything is plain
static HTML served by maat.marketing.app (GET /privacy, GET /imprint).
"""

# --- Controller identity --------------------------------------------------------------------
ORG = "Maat"  # the product / trading name
CONTACT_EMAIL = "privacy@maat.press"  # must be made deliverable (forward/route) before launch
HOSTING = "Hetzner Online GmbH (Germany, EU)"
CONTROLLER = "cauri OÜ"  # the legal entity (data controller), an Estonian private limited company
COUNTRY = "Estonia"
# TODO(cauri): registered postal address + company registration code (registrikood). An EU imprint
# wants both — they show as "to be added" until filled.
POSTAL = "Available on request via the contact address above."
UPDATED = "16 June 2026"

_FEATHER = (
    '<svg class="feather" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
    '<path d="M20 4C11 5 6 10 5 18c5 1 9-1 12-5M9 14c2-3 5-5 9-6M5 18l-2 2" stroke="currentColor" '
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

_CSS = """
:root{--paper:#f6f4ee;--ink:#16150f;--mut:#6f6a5d;--line:#e6e0d3;--gold:#a8792e;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.65 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:760px;margin:0 auto;padding:0 22px}
a{color:var(--gold)}
header.bar{position:sticky;top:0;border-bottom:1px solid var(--line);background:rgba(246,244,238,.9);backdrop-filter:blur(8px)}
header.bar .wrap{display:flex;align-items:center;height:62px}
.mark{display:flex;align-items:center;gap:9px;font-family:var(--serif);font-weight:600;font-size:21px;text-decoration:none;color:inherit}
.feather{width:20px;height:20px;color:var(--gold);flex:none}
main{padding:46px 0 60px}
h1{font-family:var(--serif);font-weight:600;font-size:clamp(30px,4vw,42px);letter-spacing:-.015em;margin:0 0 6px}
.updated{color:var(--mut);font-size:14px;margin:0 0 26px}
h2{font-family:var(--serif);font-weight:600;font-size:22px;letter-spacing:-.01em;margin:34px 0 8px}
p,li{color:#2f2d24;margin:0 0 14px}
.lead{font-size:18px}
ul{padding-left:20px;margin:0 0 14px}
li{margin:0 0 8px}
strong{color:var(--ink)}
dl{margin:0 0 14px}
dt{font-weight:600;color:var(--ink);margin-top:12px}
dd{margin:2px 0 0;color:#2f2d24}
.note{background:#fffdf8;border:1px solid var(--line);border-left:3px solid var(--gold);
  border-radius:8px;padding:12px 14px;font-size:14px;color:var(--mut);margin:0 0 22px}
footer{border-top:1px solid var(--line);padding:26px 0 48px;color:var(--mut);font-size:14px}
footer a{color:var(--mut);text-decoration:underline;text-decoration-color:var(--line)}
.dot{color:var(--gold);padding:0 6px}
"""


def _doc(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title} — Maat</title>"
        f"<style>{_CSS}</style></head><body>"
        f'<header class="bar"><div class="wrap"><a class="mark" href="/">{_FEATHER} Maat</a></div></header>'
        f'<main><div class="wrap">{body}</div></main>'
        '<footer><div class="wrap"><a href="/">← maat.press</a>'
        '<span class="dot">·</span><a href="/privacy">Privacy</a>'
        '<span class="dot">·</span><a href="/imprint">Legal notice</a></div></footer>'
        "</body></html>"
    )


_PRIVACY_BODY = f"""
<h1>Privacy Policy</h1>
<p class="updated">Last updated: {UPDATED}</p>

<p class="lead">{ORG} (“we”, “us”) runs the website <strong>maat.press</strong>. This page explains
the personal data we collect here, why, and the rights you have under the EU General Data
Protection Regulation (GDPR). We have built this site to gather as little as possible.</p>

<p><strong>This site uses no cookies, no local storage, and no third-party trackers or
advertising.</strong> The only network requests the page makes are back to our own server.</p>

<h2>Who is responsible</h2>
<p>The controller for the data described below is {CONTROLLER}, a company registered in {COUNTRY}.
For anything to do with your privacy — including any of the rights listed further down — write to
<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>. See our <a href="/imprint">Legal notice</a>
for operator details.</p>

<h2>What we collect, and why</h2>
<dl>
  <dt>1. Anonymous visit analytics — legal basis: our legitimate interest, GDPR Art. 6(1)(f)</dt>
  <dd>When you load a page we record a minimal, non-identifying entry so we can gauge interest in
  the product and improve the site:
  <ul>
    <li>the page you viewed and, if you followed a link or campaign, the referrer and any UTM tags
    in the address;</li>
    <li>a coarse device family worked out from your browser’s user-agent (for example “ios”,
    “mac”, “windows”) — we do <strong>not</strong> keep the full user-agent;</li>
    <li>a random identifier created in your browser for the current page load only, used to link a
    page view with a button tap in the same visit. It is <strong>not</strong> stored on your
    device and <strong>not</strong> tied to you.</li>
  </ul>
  We do <strong>not</strong> store your IP address with these entries, and we cannot identify you
  from them.</dd>

  <dt>2. The launch list — legal basis: your consent, GDPR Art. 6(1)(a)</dt>
  <dd>If you ask us to tell you when Maat launches, we store the email address you give us and,
  only if you tick the box, that you would like to be a <strong>beta tester</strong>. We use this
  solely to send you the launch notification (and beta-testing information if you opted in). We do
  not sell it, share it, or use it for anything else. You can withdraw at any time — see below.</dd>
</dl>

<h2>How long we keep it</h2>
<p>Analytics entries are retained only as long as they are useful for the purpose above.
Launch-list emails are kept until launch and a short period afterwards, or until you ask us to
remove you — whichever comes first.</p>

<h2>Who we share it with</h2>
<p>We do not sell or share your personal data. The site and database are hosted in the European
Union by {HOSTING}, acting as our processor under contract. We do not transfer your data outside
the EU/EEA.</p>

<h2>Your rights</h2>
<p>Under the GDPR you have the right to access your data, to have it corrected or erased (the
“right to be forgotten”), to restrict or object to our processing, and to data portability. Where
we rely on your consent, you may withdraw it at any time without affecting what came before. To
exercise any of these, email <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a> and we will
respond. You also have the right to lodge a complaint with your local data-protection supervisory
authority.</p>

<h2>Changes to this policy</h2>
<p>If this policy changes, the “last updated” date above will change with it.</p>
"""


_IMPRINT_BODY = f"""
<h1>Legal notice</h1>
<p class="updated">Last updated: {UPDATED}</p>

<div class="note">Registered postal address and company registration code (registrikood) will
be added here. For any legal or data-protection matter in the meantime, use the contact address
below.</div>

<dl>
  <dt>Service</dt><dd>maat.press</dd>
  <dt>Operator</dt><dd>{CONTROLLER}, a private limited company (osaühing) registered in {COUNTRY}</dd>
  <dt>Contact</dt><dd><a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a></dd>
  <dt>Postal address</dt><dd>{POSTAL}</dd>
  <dt>Hosting provider</dt><dd>{HOSTING}</dd>
</dl>

<p>For how we handle personal data, see our <a href="/privacy">Privacy Policy</a>.</p>
"""


PRIVACY = _doc("Privacy Policy", _PRIVACY_BODY)
IMPRINT = _doc("Legal notice", _IMPRINT_BODY)
