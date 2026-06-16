"""The maat.press landing page — one self-contained document (HTML + CSS + JS).

Embedded as a string for the same reason the console keeps `_DOC` inline: it ships with the
package, no static-file mounting or build step. The page is first-party and cookieless — the
only network calls it makes are POSTs back to this same service (/track/view, /track/click,
/notify). No third-party trackers, no ad scripts (D9 — EU-sovereign, privacy by default).
"""

# The visitor funnel is recorded by the inline script at the bottom: a page view on load, a
# "Download on the App Store" tap (which opens the coming-soon dialog), and an optional
# launch-notify email. Each posts JSON to this service; the service publishes the event.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maat — news weighted by truth, not volume</title>
<meta name="description" content="Maat reads across the open web in English, Portuguese and French, scores how well each story holds up — corroboration over spread — and attaches a confidence read to everything it surfaces.">
<meta property="og:title" content="Maat — news weighted by truth, not volume">
<meta property="og:description" content="Corroboration over spread. A confidence read on every story. A genuinely wider lens.">
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

/* buttons */
.appstore{display:inline-flex;align-items:center;gap:10px;background:var(--ink);color:#fff;
  text-decoration:none;border:0;cursor:pointer;font:600 15px/1 var(--sans);
  padding:13px 20px;border-radius:12px;transition:transform .12s ease,box-shadow .12s ease;
  box-shadow:0 1px 2px rgba(0,0,0,.18)}
.appstore:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(0,0,0,.16)}
.appstore .apple{width:18px;height:18px;flex:none}
.appstore small{display:block;font-size:10px;font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;opacity:.72;margin-bottom:2px}
.appstore b{font-size:15px;font-weight:600}
.appstore.pill{padding:9px 16px;border-radius:10px}
.ghost{display:inline-block;margin-top:14px;color:var(--mut);font-size:14px;
  text-decoration:underline;text-decoration-color:var(--line);cursor:pointer}
.ghost:hover{color:var(--ink)}

/* hero */
.hero{padding:64px 0 28px}
.hero .grid{display:grid;grid-template-columns:1fr;gap:40px;align-items:center}
@media(min-width:900px){.hero .grid{grid-template-columns:1.05fr .95fr;gap:54px}.hero{padding:84px 0 44px}}
.kicker{font-size:13px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
  color:var(--gold);margin:0 0 18px}
h1{font-family:var(--serif);font-weight:600;font-size:clamp(34px,5.2vw,58px);line-height:1.04;
  letter-spacing:-.015em;margin:0 0 18px}
.lede{font-size:clamp(17px,2.1vw,20px);color:#3a382f;margin:0 0 26px;max-width:33ch}
.cta-note{margin:12px 0 0;font-size:13px;color:var(--mut)}
.cta-note b{color:var(--ink);font-weight:600}

/* hero story-card mock */
.demo{background:var(--card);border:1px solid var(--line);border-radius:18px;
  padding:22px 22px 18px;box-shadow:0 24px 60px -30px rgba(40,32,12,.45);max-width:430px}
.demo .src{font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--mut)}
.demo h3{font-family:var(--serif);font-weight:600;font-size:21px;line-height:1.22;margin:7px 0 14px}
.conf{display:flex;align-items:center;gap:10px;margin:0 0 4px}
.conf .pct{font-weight:700;font-variant-numeric:tabular-nums;color:var(--green);min-width:42px}
/* the confidence meter — a distinct class from header.bar so it can't collapse the sticky header */
.meter{flex:1;height:8px;background:#ece7da;border-radius:6px;overflow:hidden}
.meter i{display:block;height:100%;width:92%;background:var(--green);border-radius:6px}
.conf .lab{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;
  color:var(--green);background:var(--green-wash);padding:2px 9px;border-radius:20px}
.demo .why{font-size:13px;color:var(--mut);margin:12px 0 14px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:12px;font-weight:600;padding:3px 10px;border-radius:20px;background:#f1ede2;color:#55513f}
.chip.primary{background:var(--gold-wash);color:#7a5618}

/* section rhythm */
section.band{padding:62px 0;border-top:1px solid var(--line)}
.eyebrow{font-size:13px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:var(--gold);margin:0 0 12px;text-align:center}
.band h2{font-family:var(--serif);font-weight:600;font-size:clamp(26px,3.6vw,38px);line-height:1.12;
  letter-spacing:-.01em;margin:0 auto 12px;text-align:center;max-width:18ch}
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

/* spread explainer */
.spread{display:grid;grid-template-columns:1fr;gap:26px;align-items:center;margin-top:36px}
@media(min-width:820px){.spread{grid-template-columns:1fr 1fr;gap:48px}}
.spread .lead{font-family:var(--serif);font-size:22px;line-height:1.4;letter-spacing:-.005em}
.spread .lead b{color:var(--gold)}
.collapse{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px}
.echo{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:14px}
.echo span{width:13px;height:13px;border-radius:3px;background:#e7e1d3}
.echo span.o{background:var(--gold)}
.collapse .row{display:flex;align-items:center;justify-content:space-between;font-size:14px;color:var(--mut);
  padding-top:12px;border-top:1px dashed var(--line)}
.collapse .row b{color:var(--ink);font-weight:700;font-variant-numeric:tabular-nums}

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
      <a href="#how">How it judges</a>
      <button class="appstore pill" data-cta="ios" type="button">
        <svg class="apple" viewBox="0 0 384 512" fill="currentColor" aria-hidden="true"><path d="M318.7 268.7c-.2-36.7 16.4-64.4 50-84.8-18.8-26.9-47.2-41.7-84.7-44.6-35.5-2.8-74.3 20.7-88.5 20.7-15 0-49.4-19.7-76.4-19.7C73.3 141.2 24 184.8 24 273.5q0 39.3 14.4 81.2c12.8 36.7 59 126.7 107.2 125.2 25.2-.6 43-17.9 75.8-17.9 31.8 0 48.3 17.9 76.4 17.9 48.6-.7 90.4-82.5 102.6-119.3-65.2-30.7-61.7-90-61.7-91.9zm-56.6-164.2c27.3-32.4 24.8-61.9 24-72.5-24.1 1.4-52 16.4-67.9 34.9-17.5 19.8-27.8 44.3-25.6 71.9 26.1 2 49.9-11.4 69.5-34.3z"/></svg>
        <b>Download</b>
      </button>
    </nav>
  </div>
</header>

<main>
  <section class="hero">
    <div class="wrap">
      <div class="grid">
        <div>
          <p class="kicker">A veracity-weighted news feed</p>
          <h1>News weighted by truth, not volume.</h1>
          <p class="lede">Maat reads across the open web in English, Portuguese and French, scores how well each story actually holds up — corroboration over spread — and attaches a confidence read to everything it surfaces.</p>
          <button class="appstore" data-cta="ios" type="button">
            <svg class="apple" viewBox="0 0 384 512" fill="currentColor" aria-hidden="true"><path d="M318.7 268.7c-.2-36.7 16.4-64.4 50-84.8-18.8-26.9-47.2-41.7-84.7-44.6-35.5-2.8-74.3 20.7-88.5 20.7-15 0-49.4-19.7-76.4-19.7C73.3 141.2 24 184.8 24 273.5q0 39.3 14.4 81.2c12.8 36.7 59 126.7 107.2 125.2 25.2-.6 43-17.9 75.8-17.9 31.8 0 48.3 17.9 76.4 17.9 48.6-.7 90.4-82.5 102.6-119.3-65.2-30.7-61.7-90-61.7-91.9zm-56.6-164.2c27.3-32.4 24.8-61.9 24-72.5-24.1 1.4-52 16.4-67.9 34.9-17.5 19.8-27.8 44.3-25.6 71.9 26.1 2 49.9-11.4 69.5-34.3z"/></svg>
            <span><small>Download on the</small><b>App Store</b></span>
          </button>
          <p class="cta-note"><b>iPhone &amp; Mac.</b> Coming soon — <span class="ghost" data-cta="mac">join the launch list</span>.</p>
        </div>
        <div>
          <div class="demo" aria-hidden="true">
            <div class="src">Corroborated · 6 languages</div>
            <h3>Coalition agrees framework to slow cross-border arms flow</h3>
            <div class="conf">
              <span class="pct">92%</span>
              <span class="meter"><i></i></span>
              <span class="lab">Well corroborated</span>
            </div>
            <div class="why">3 independent originators · 1 primary source · weighed against prior</div>
            <div class="chips">
              <span class="chip primary">Filed document · primary</span>
              <span class="chip">AFP</span>
              <span class="chip">Folha de S.Paulo</span>
              <span class="chip">Le Monde</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <section class="band" id="why">
    <div class="wrap">
      <p class="eyebrow">Why Maat</p>
      <h2>Built to trust corroborated truth over loud consensus.</h2>
      <p class="sub">Most feeds rank by reach. Maat asks a different question of every story: how well does it actually hold up?</p>
      <div class="pillars">
        <div class="pillar">
          <div class="n">1</div>
          <h3>Loud isn't true</h3>
          <p>Maat counts how many <em>independent</em> sources stand a story up. Wire reprints, citation cascades and same-owner outlets collapse to a single voice — thirty echoes of one unverified figure count as one thin thread, not thirty.</p>
        </div>
        <div class="pillar">
          <div class="n">2</div>
          <h3>A confidence read on every story</h3>
          <p>Not a black box, and not a feed someone curated for you. Every story carries a confidence read you can see — and open, to find exactly why it's rated the way it is.</p>
        </div>
        <div class="pillar">
          <div class="n">3</div>
          <h3>A genuinely wider lens</h3>
          <p>Most apps quietly centre the US and the Anglo-American press. Maat weights against that, drawing on sources across languages and regions, so world news reads like the world — not one corner of it.</p>
        </div>
        <div class="pillar">
          <div class="n">4</div>
          <h3>Sources earn trust by being right</h3>
          <p>Maat tracks whether sources tell the truth over time — judged against what actually proved out, never against the consensus of the moment. The outlet that breaks a true story early is rewarded, not punished for leaving the herd.</p>
        </div>
        <div class="pillar span2">
          <div class="n">5</div>
          <h3>Yours, and private</h3>
          <p>Tell Maat your topics in plain language. It tunes the feed and re-ranks to your taste on your own device — your reading stays with you. Built in Europe, with no trackers and no ads.</p>
        </div>
      </div>
    </div>
  </section>

  <section class="band" id="how">
    <div class="wrap">
      <p class="eyebrow">How it judges</p>
      <h2>Corroboration over spread.</h2>
      <div class="spread">
        <p class="lead">A hundred outlets reprinting one wire story isn't a hundred witnesses. It's <b>one</b>. Maat finds the independent originators behind a claim and weights primary sources — a named official, a filed document, on-the-ground reporting — above any amount of secondary repetition.</p>
        <div class="collapse">
          <div class="echo">
            <span class="o"></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span>
          </div>
          <div class="row"><span>30 outlets carried it</span><b>1 original thread</b></div>
        </div>
      </div>
    </div>
  </section>

  <section class="band close-cta">
    <div class="wrap">
      <h2>Read the news by how well it holds up.</h2>
      <button class="appstore" data-cta="ios" type="button">
        <svg class="apple" viewBox="0 0 384 512" fill="currentColor" aria-hidden="true"><path d="M318.7 268.7c-.2-36.7 16.4-64.4 50-84.8-18.8-26.9-47.2-41.7-84.7-44.6-35.5-2.8-74.3 20.7-88.5 20.7-15 0-49.4-19.7-76.4-19.7C73.3 141.2 24 184.8 24 273.5q0 39.3 14.4 81.2c12.8 36.7 59 126.7 107.2 125.2 25.2-.6 43-17.9 75.8-17.9 31.8 0 48.3 17.9 76.4 17.9 48.6-.7 90.4-82.5 102.6-119.3-65.2-30.7-61.7-90-61.7-91.9zm-56.6-164.2c27.3-32.4 24.8-61.9 24-72.5-24.1 1.4-52 16.4-67.9 34.9-17.5 19.8-27.8 44.3-25.6 71.9 26.1 2 49.9-11.4 69.5-34.3z"/></svg>
        <span><small>Download on the</small><b>App Store</b></span>
      </button>
      <p class="cta-note"><b>iPhone &amp; Mac.</b> Coming soon — <span class="ghost" data-cta="mac">join the launch list</span>.</p>
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
      <div class="meta">Ma'at — the ancient Egyptian principle of truth, balance and order, whose feather a heart was weighed against. The claim, not the headline, is our unit.</div>
    </div>
    <div class="meta">
      Built in Europe<span class="dot">·</span>No trackers, no ads<br>
      English<span class="dot">·</span>Português<span class="dot">·</span>Français<br>
      <a href="/privacy">Privacy</a><span class="dot">·</span><a href="/imprint">Legal notice</a><br>
      &copy; 2026 Maat
    </div>
  </div>
</footer>

<dialog id="soon" aria-label="Coming soon">
  <div class="modal-wrap">
    <button class="x" data-close type="button" aria-label="Close">&times;</button>
    <div class="modal">
      <svg class="feather" viewBox="0 0 24 24" fill="none" aria-hidden="true" style="color:var(--gold)">
        <path d="M20 4C11 5 6 10 5 18c5 1 9-1 12-5M9 14c2-3 5-5 9-6M5 18l-2 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <h3>Coming soon</h3>
      <p>Maat is in private development. Want us to tell you the moment it's on the App Store?</p>
      <form class="notify" id="notify" novalidate>
        <input type="email" name="email" placeholder="you@example.com" autocomplete="email" required>
        <button type="submit">Notify me</button>
      </form>
      <label class="optin"><input type="checkbox" id="beta">
        I’d also like to be a beta tester and help shape the app before launch.</label>
      <p class="note" id="notify-note"></p>
      <p class="tiny">One email, when it launches. Nothing else. By submitting you agree to our
        <a href="/privacy" target="_blank" rel="noopener" style="color:var(--gold)">Privacy Policy</a>.</p>
    </div>
  </div>
</dialog>

<script>
(function(){
  var V = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now();
  var params = new URLSearchParams(location.search);
  var lastPlatform = "ios";
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

  var dlg = document.getElementById("soon");
  function openSoon(platform){
    lastPlatform = platform || "ios";
    send("/track/click", ctx({platform: lastPlatform}));
    if(dlg && dlg.showModal){ try{ dlg.showModal(); }catch(e){} }
  }
  [].forEach.call(document.querySelectorAll("[data-cta]"), function(b){
    b.addEventListener("click", function(e){ e.preventDefault(); openSoon(b.getAttribute("data-cta")); });
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
      send("/notify", ctx({email: email, platform: lastPlatform, beta: !!(betaBox && betaBox.checked)})).then(function(r){
        return r ? r.json() : {ok:true};
      }).then(function(j){
        if(j && j.ok){
          form.outerHTML = "<p class='thanks'>Thanks — we'll let you know the moment it's live.</p>";
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
