"""The maat.press landing page — one self-contained document (HTML + CSS + JS).

Embedded as a string for the same reason the console keeps `_DOC` inline: it ships with the
package, no static-file mounting or build step. The page is first-party and cookieless — the
only network calls it makes are POSTs back to this same service (/track/view, /track/click,
/notify). No third-party trackers, no ad scripts (D9 — EU-sovereign, privacy by default).

Copy rule (cauri): say WHAT Maat does — it scores how reliable each source is, built from how
accurate their reporting proves over time, and surfaces that on every story — speaking to the
value for the reader. Never say HOW (corroboration, independent originators, primary sources,
the anti-spread weighting): that method is private and describing it would help bad actors game
it. Maat reads the news in every language; never claim a fixed short list.
"""

# The visitor funnel is recorded by the inline script at the bottom: a page view on load, an
# early-access tap (which opens the access dialog), and an optional email + beta opt-in. Each
# posts JSON to this service; the service publishes the event.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maat — know which news to trust</title>
<meta name="description" content="Maat scores how reliable each news source is — built from how accurate their reporting proves over time — and puts that signal on every story, so you can read the news knowing what stands up.">
<meta property="og:title" content="Maat — know which news to trust">
<meta property="og:description" content="A reliability score on every source, earned by being accurate over time. Read the news knowing what holds up — in every language, from across the world.">
<meta property="og:type" content="website">
<style>
:root{
  --paper:#f6f4ee; --ink:#16150f; --mut:#6f6a5d; --line:#e6e0d3;
  --card:#fffdf8; --gold:#a8792e; --gold-wash:#f3ead3;
  --green:#3b6d11; --green-wash:#eaf3de;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.6 var(--sans);
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:inherit}
.wrap{max-width:1060px;margin:0 auto;padding:0 22px}
.mark{display:flex;align-items:center;gap:9px;font-family:var(--serif);font-weight:600;
  font-size:21px;letter-spacing:.01em;text-decoration:none}
.feather{width:20px;height:20px;color:var(--gold);flex:none}

/* top bar */
header.bar{position:sticky;top:0;z-index:10;background:rgba(246,244,238,.86);
  backdrop-filter:saturate(1.2) blur(8px);border-bottom:1px solid var(--line)}
header.bar .wrap{display:flex;align-items:center;justify-content:space-between;height:62px}
.bar nav{display:flex;align-items:center;gap:24px;font-size:14px;color:var(--mut)}
.bar nav a{text-decoration:none}
.bar nav a:hover{color:var(--ink)}
.bar .pill{display:none}
@media(min-width:680px){.bar .pill{display:inline-flex}}

/* primary button */
.cta{display:inline-flex;align-items:center;gap:8px;background:var(--ink);color:#fff;
  text-decoration:none;border:0;cursor:pointer;font:600 15px/1 var(--sans);
  padding:14px 22px;border-radius:12px;transition:transform .12s ease,box-shadow .12s ease;
  box-shadow:0 1px 2px rgba(0,0,0,.18)}
.cta:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(0,0,0,.16)}
.cta.pill{padding:10px 16px;border-radius:10px;font-size:14px}
.cta-note{margin:12px 0 0;font-size:13px;color:var(--mut)}
.cta-note b{color:var(--ink);font-weight:600}

/* hero */
.hero{padding:64px 0 28px}
.hero .grid{display:grid;grid-template-columns:1fr;gap:40px;align-items:center}
@media(min-width:900px){.hero .grid{grid-template-columns:1.05fr .95fr;gap:54px}.hero{padding:84px 0 44px}}
.kicker{font-size:13px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
  color:var(--gold);margin:0 0 18px}
h1{font-family:var(--serif);font-weight:600;font-size:clamp(34px,5.2vw,58px);line-height:1.04;
  letter-spacing:-.015em;margin:0 0 18px}
.lede{font-size:clamp(17px,2.1vw,20px);color:#3a382f;margin:0 0 26px;max-width:34ch}

/* hero story-card mock */
.demo{background:var(--card);border:1px solid var(--line);border-radius:18px;
  padding:22px 22px 18px;box-shadow:0 24px 60px -30px rgba(40,32,12,.45);max-width:430px}
.demo .src{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;
  letter-spacing:.07em;text-transform:uppercase;color:var(--mut)}
.demo .src .score{color:var(--green);background:var(--green-wash);padding:2px 8px;border-radius:20px}
.demo h3{font-family:var(--serif);font-weight:600;font-size:21px;line-height:1.22;margin:9px 0 14px}
.conf{display:flex;align-items:center;gap:10px;margin:0 0 4px}
.conf .pct{font-weight:700;font-variant-numeric:tabular-nums;color:var(--green);min-width:42px}
/* the reliability meter — a distinct class from header.bar so it can't collapse the sticky header */
.meter{flex:1;height:8px;background:#ece7da;border-radius:6px;overflow:hidden}
.meter i{display:block;height:100%;width:92%;background:var(--green);border-radius:6px}
.conf .lab{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;
  color:var(--green);background:var(--green-wash);padding:2px 9px;border-radius:20px}
.demo .why{font-size:13px;color:var(--mut);margin:12px 0 2px}

/* section rhythm */
section.band{padding:62px 0;border-top:1px solid var(--line)}
.eyebrow{font-size:13px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:var(--gold);margin:0 0 12px;text-align:center}
.band h2{font-family:var(--serif);font-weight:600;font-size:clamp(26px,3.6vw,38px);line-height:1.12;
  letter-spacing:-.01em;margin:0 auto 12px;text-align:center;max-width:20ch}
.band .sub{text-align:center;color:var(--mut);max-width:60ch;margin:0 auto 8px;font-size:17px}

/* pillars */
.pillars{display:grid;grid-template-columns:1fr;gap:16px;margin-top:40px}
@media(min-width:720px){.pillars{grid-template-columns:1fr 1fr}}
.pillar{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:24px 24px 22px}
.pillar .n{font-family:var(--serif);font-size:15px;font-weight:700;color:var(--gold);
  width:34px;height:34px;border:1.5px solid var(--gold-wash);border-radius:10px;
  display:flex;align-items:center;justify-content:center;margin-bottom:14px;background:#fff}
.pillar h3{font-family:var(--serif);font-weight:600;font-size:21px;letter-spacing:-.01em;margin:0 0 7px}
.pillar p{margin:0;color:#42402f;font-size:15.5px;line-height:1.55}
.pillar.span2{grid-column:1/-1}

/* closing */
.close-cta{text-align:center}
.close-cta h2{margin-bottom:22px}

/* footer */
footer{border-top:1px solid var(--line);padding:40px 0 56px;color:var(--mut);font-size:14px}
footer .wrap{display:flex;flex-wrap:wrap;gap:18px;justify-content:space-between;align-items:flex-start}
footer .name{max-width:46ch}
footer .name .mark{margin-bottom:8px;font-size:18px}
footer .meta{font-size:13px;line-height:1.7}
footer .meta a{color:var(--mut);text-decoration:underline;text-decoration-color:var(--line)}
footer .meta a:hover{color:var(--ink)}
footer .dot{color:var(--gold);padding:0 6px}

/* dialog */
dialog{border:1px solid var(--line);border-radius:18px;padding:0;max-width:440px;width:calc(100% - 36px);
  background:var(--card);color:var(--ink);box-shadow:0 30px 80px -24px rgba(30,22,6,.5)}
dialog::backdrop{background:rgba(22,21,15,.42);backdrop-filter:blur(2px)}
.modal{padding:28px 26px 24px}
.modal .feather{width:26px;height:26px;margin-bottom:10px}
.modal h3{font-family:var(--serif);font-weight:600;font-size:25px;margin:0 0 8px;letter-spacing:-.01em}
.modal p{margin:0 0 18px;color:#46442f;font-size:15.5px}
form.notify{display:flex;gap:8px}
form.notify input{flex:1;font:inherit;font-size:15px;padding:12px 13px;border:1px solid var(--line);
  border-radius:11px;background:#fff;color:var(--ink)}
form.notify input:focus{outline:2px solid var(--gold-wash);border-color:var(--gold)}
form.notify button{font:600 15px/1 var(--sans);padding:0 18px;border:0;border-radius:11px;
  background:var(--ink);color:#fff;cursor:pointer}
.optin{display:flex;align-items:flex-start;gap:9px;margin:13px 2px 0;font-size:14px;
  color:#46442f;cursor:pointer;line-height:1.4}
.optin input{width:17px;height:17px;margin-top:1px;flex:none;accent-color:var(--gold);cursor:pointer}
.note{font-size:13px;color:var(--mut);margin:10px 2px 0;min-height:18px}
.note.err{color:#b3402e}
.thanks{font-size:16px;color:var(--green);margin:6px 2px;font-weight:600}
.modal .x{position:absolute;top:12px;right:14px;border:0;background:none;color:var(--mut);
  font-size:22px;line-height:1;cursor:pointer;padding:6px}
.modal-wrap{position:relative}
.tiny{font-size:12px;color:var(--mut);margin-top:14px}
</style>
</head>
<body>

<header class="bar">
  <div class="wrap">
    <a class="mark" href="/">
      <svg class="feather" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M20 4C11 5 6 10 5 18c5 1 9-1 12-5M9 14c2-3 5-5 9-6M5 18l-2 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      Maat
    </a>
    <nav>
      <a href="#why">Why Maat</a>
      <button class="cta pill" data-cta="nav" type="button">Get early access</button>
    </nav>
  </div>
</header>

<main>
  <section class="hero">
    <div class="wrap">
      <div class="grid">
        <div>
          <p class="kicker">A veracity-weighted news feed</p>
          <h1>Know which news to trust.</h1>
          <p class="lede">Maat scores how reliable each source is — built from how accurate their reporting proves over time — and puts that signal on every story. Read the news knowing what stands up.</p>
          <button class="cta" data-cta="hero" type="button">Get early access</button>
          <p class="cta-note"><b>For iPhone &amp; Mac.</b> Be among the first to read it.</p>
        </div>
        <div>
          <div class="demo" aria-hidden="true">
            <div class="src">Le Monde <span class="score">96% reliable source</span></div>
            <h3>Coalition agrees framework to slow cross-border arms flow</h3>
            <div class="conf">
              <span class="pct">92%</span>
              <span class="meter"><i></i></span>
              <span class="lab">Trust this</span>
            </div>
            <div class="why">Scored on this source's track record of accurate reporting.</div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <section class="band" id="why">
    <div class="wrap">
      <p class="eyebrow">Why Maat</p>
      <h2>The news, with a reliability score you can see.</h2>
      <p class="sub">Most feeds rank by reach. Maat asks the only question that matters of every story: can you trust it?</p>
      <div class="pillars">
        <div class="pillar">
          <div class="n">1</div>
          <h3>A reliability score on every source</h3>
          <p>Maat rates how much you can trust each outlet — and, soon, the individual writers and contributors behind a story. No more guessing who's worth believing.</p>
        </div>
        <div class="pillar">
          <div class="n">2</div>
          <h3>Reputation is earned, not assumed</h3>
          <p>Sources earn their score by being right. The ones with a track record of accurate reporting rise; the ones that keep getting it wrong fall. Trust follows the truth — over time, not the loudest moment.</p>
        </div>
        <div class="pillar">
          <div class="n">3</div>
          <h3>A trust read on every story</h3>
          <p>Every story carries a clear, visible signal of how much it holds up — so you can tell at a glance what to rely on, instead of guessing or taking it on faith.</p>
        </div>
        <div class="pillar">
          <div class="n">4</div>
          <h3>The whole world, every language</h3>
          <p>Maat reads the news in every language, from sources across the globe — so you see the full picture, not just the English-language press or one corner of the world.</p>
        </div>
        <div class="pillar span2">
          <div class="n">5</div>
          <h3>Yours, and private</h3>
          <p>Tell Maat your interests in plain language; it tunes your feed to your taste on your own device — your reading stays with you. Built in Europe, with no trackers and no ads.</p>
        </div>
      </div>
    </div>
  </section>

  <section class="band close-cta">
    <div class="wrap">
      <h2>Read the news knowing what to trust.</h2>
      <button class="cta" data-cta="footer" type="button">Get early access</button>
      <p class="cta-note"><b>For iPhone &amp; Mac.</b> Be among the first to read it.</p>
    </div>
  </section>
</main>

<footer>
  <div class="wrap">
    <div class="name">
      <a class="mark" href="/">
        <svg class="feather" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M20 4C11 5 6 10 5 18c5 1 9-1 12-5M9 14c2-3 5-5 9-6M5 18l-2 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        Maat
      </a>
      <div class="meta">Ma'at — the ancient Egyptian principle of truth, balance and order, whose feather a heart was weighed against. Trust, weighed honestly, is our whole purpose.</div>
    </div>
    <div class="meta">
      Built in Europe<span class="dot">·</span>No trackers, no ads<br>
      Every language, from across the world<br>
      <a href="/privacy">Privacy</a><span class="dot">·</span><a href="/imprint">Legal notice</a><br>
      &copy; 2026 Maat
    </div>
  </div>
</footer>

<dialog id="access" aria-label="Get early access">
  <div class="modal-wrap">
    <button class="x" data-close type="button" aria-label="Close">&times;</button>
    <div class="modal">
      <svg class="feather" viewBox="0 0 24 24" fill="none" aria-hidden="true" style="color:var(--gold)">
        <path d="M20 4C11 5 6 10 5 18c5 1 9-1 12-5M9 14c2-3 5-5 9-6M5 18l-2 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <h3>Get early access</h3>
      <p>Leave your email and we'll send your invitation to Maat.</p>
      <form class="notify" id="notify" novalidate>
        <input type="email" name="email" placeholder="you@example.com" autocomplete="email" required>
        <button type="submit">Request access</button>
      </form>
      <label class="optin"><input type="checkbox" id="beta">
        I'd also like to be a beta tester and help shape Maat before launch.</label>
      <p class="note" id="notify-note"></p>
      <p class="tiny">We'll only use your email to get you access. Nothing else. By submitting you agree to our
        <a href="/privacy" target="_blank" rel="noopener" style="color:var(--gold)">Privacy Policy</a>.</p>
    </div>
  </div>
</dialog>

<script>
(function(){
  var V = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now();
  var params = new URLSearchParams(location.search);
  function ctx(extra){
    var d = {
      path: location.pathname,
      referrer: document.referrer || "",
      utm_source: params.get("utm_source") || "",
      utm_medium: params.get("utm_medium") || "",
      utm_campaign: params.get("utm_campaign") || "",
      visitor: V
    };
    if(extra){ for(var k in extra){ d[k] = extra[k]; } }
    return d;
  }
  function send(url, body){
    try{
      return fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify(body), keepalive:true});
    }catch(e){ return Promise.resolve(null); }
  }
  send("/track/view", ctx());

  var dlg = document.getElementById("access");
  function openAccess(where){
    send("/track/click", ctx({platform: where || "hero"}));
    if(dlg && dlg.showModal){ try{ dlg.showModal(); }catch(e){} }
  }
  [].forEach.call(document.querySelectorAll("[data-cta]"), function(b){
    b.addEventListener("click", function(e){ e.preventDefault(); openAccess(b.getAttribute("data-cta")); });
  });
  [].forEach.call(document.querySelectorAll("[data-close]"), function(b){
    b.addEventListener("click", function(){ if(dlg) dlg.close(); });
  });
  if(dlg){ dlg.addEventListener("click", function(e){ if(e.target === dlg) dlg.close(); }); }

  var form = document.getElementById("notify");
  if(form){
    form.addEventListener("submit", function(e){
      e.preventDefault();
      var input = form.querySelector("input[type=email]");
      var betaBox = document.getElementById("beta");
      var note = document.getElementById("notify-note");
      var email = (input.value || "").trim();
      if(email.indexOf("@") < 1 || email.lastIndexOf(".") < email.indexOf("@")){
        note.textContent = "Please enter a valid email."; note.className = "note err"; return;
      }
      note.textContent = "Sending…"; note.className = "note";
      send("/notify", ctx({email: email, beta: !!(betaBox && betaBox.checked)})).then(function(r){
        return r ? r.json() : {ok:true};
      }).then(function(j){
        if(j && j.ok){
          form.outerHTML = "<p class='thanks'>You're on the list — we'll be in touch with your access.</p>";
        } else {
          note.textContent = (j && j.error) || "Something went wrong — try again."; note.className = "note err";
        }
      }).catch(function(){
        note.textContent = "Something went wrong — try again."; note.className = "note err";
      });
    });
  }
})();
</script>
</body>
</html>"""
