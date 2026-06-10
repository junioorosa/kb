/* KB Manager — "Core" explainer view.
 *
 * Self-contained drop-in: this one file injects its own stylesheet <link>, its
 * own nav tab, and the whole #view-core panel, then wires the animations. It
 * borrows nothing from app.js and edits nothing — a delegated nav listener
 * handles showing/hiding the panel, so app.js's id-specific switchView stays
 * untouched. Bootstrapped by a single <script> tag in index.html; if that file
 * is ever absent the tag 404s and nothing runs — no broken tab.
 *
 * Numbers in the copy are the real engine constants (kb_retrieve.py / kb-embed):
 *   hybrid = (0.7*cosine + 0.3*BM25norm) * scope * status
 *   scope  = workspace 1.30 / project 1.20 / ticket 1.00 / index 1.05
 *   status = experimental 0.4 / discarded 0.0
 *   hybrid tiers: high >= 0.70, mid 0.45-0.70, low < 0.45 (BM25-only: 8.0 / 5.0)
 *   model  = paraphrase-multilingual-MiniLM-L12-v2, 384d, offline (fastembed)
 *   daemon = 127.0.0.1:47821 (loopback)
 */
(function () {
  "use strict";

  var TOKEN = new URLSearchParams(location.search).get("t") || "";
  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function api(path) {
    return fetch(path, { headers: { "X-KB-Token": TOKEN } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); });
  }

  // ---- the panel markup -------------------------------------------------
  function panelHTML() {
    return [
      '<div class="core">',

      // ===== HERO =====
      '<section class="core-hero">',
      '  <div class="core-hero-grid"></div>',
      '  <div class="core-hero-inner core-reveal is-visible">',
      '    <span class="core-eyebrow"><span class="dotpulse"></span> the KB core, end to end</span>',
      '    <h1 class="core-title">Your engineering knowledge,<br><span class="grad">captured and fed back</span> on its own.</h1>',
      '    <p class="core-thesis">Solving a ticket produces knowledge that usually <b>evaporates</b> — it leaves your head, leaves the team, becomes re-investigation. KB distills that knowledge <b>from git and from your conversation with the agent</b>, and injects it back <b>at the exact moment</b> you ask something related. You never write the doc. You never remember to search.</p>',
      '    <div class="core-livebar">',
      '      <div class="core-stat"><div class="n" id="cs-learnings">—</div><div class="l">learnings in the vault</div></div>',
      '      <div class="core-stat"><div class="n" id="cs-tickets">—</div><div class="l">tickets</div></div>',
      '      <div class="core-stat"><div class="n small" id="cs-daemon">—</div><div class="l">index in RAM</div></div>',
      '      <div class="core-stat"><div class="n small" id="cs-sync">—</div><div class="l">last sync</div></div>',
      '    </div>',
      '  </div>',
      '</section>',

      // ===== 1. THE GOAL =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">the goal</div>',
      '  <h2 class="core-h">An engineering memory that maintains itself</h2>',
      '  <p class="core-lead">The goal is not "yet another wiki" nobody updates. It\'s a <span class="hl">closed loop</span>: you work, KB learns from what landed in git, and on your next question the learning comes back to you. Three pieces make that happen.</p></div>',
      '  <div class="core-loop core-reveal">',
      '    <div class="core-loop-ring"></div>',
      '    <div class="core-orbit-dot"></div>',
      '    <div class="core-loop-center"><span class="lc-a">work</span><span class="lc-arrow">↓</span><span class="lc-b">memory</span></div>',
      '    <div class="core-node" style="--x:0px;--y:-128px"><div class="cn-i">⌨️</div><div class="cn-t">Work</div><div class="cn-s">commits + conversation</div></div>',
      '    <div class="core-node" style="--x:128px;--y:0px"><div class="cn-i">🌙</div><div class="cn-t">Capture</div><div class="cn-s">git + conversation</div></div>',
      '    <div class="core-node" style="--x:0px;--y:128px"><div class="cn-i">🧩</div><div class="cn-t">Index</div><div class="cn-s">embeddings + BM25</div></div>',
      '    <div class="core-node" style="--x:-128px;--y:0px"><div class="cn-i">⚡</div><div class="cn-t">Recall</div><div class="cn-s">injected into the prompt</div></div>',
      '  </div>',
      '  <div class="core-grid2 core-reveal">',
      '    <div class="pillar" style="--c:var(--core-violet)"><div class="p-i">🌙</div><h4>Sync</h4><p>Every night it reads git <b>and your conversation with the agent</b> and <b>distills learnings</b> from the work that landed. You don\'t document — it documents for you.</p><span class="p-tag">git + conversation → markdown</span></div>',
      '    <div class="pillar" style="--c:var(--core-cyan)"><div class="p-i">🧩</div><h4>Index</h4><p>Keeps the vault in RAM as <b>vectors + terms</b>. Semantic and literal, ready to search in milliseconds.</p><span class="p-tag">embeddings + BM25</span></div>',
      '    <div class="pillar" style="--c:var(--core-blue)"><div class="p-i">⚡</div><h4>Recall</h4><p>On every prompt, finds what\'s relevant and <b>injects it into context</b> before the LLM answers. Zero clicks.</p><span class="p-tag">&lt;vault-context&gt;</span></div>',
      '  </div>',
      '</section>',

      // ===== 2. PIPELINE (centerpiece) =====
      '<section class="core-section" id="core-flow-sec">',
      '  <div class="core-reveal"><div class="core-kicker">what happens when you send a prompt</div>',
      '  <h2 class="core-h">The recall flow, step by step</h2>',
      '  <p class="core-lead">Before the LLM sees a single word, a hook (<span class="hl">UserPromptSubmit</span>) intercepts your prompt and runs this pipeline. All local — <b>no API call</b> in your prompt\'s path.</p></div>',
      '  <div class="flow-wrap core-reveal">',
      '    <div class="flow-head">',
      '      <span class="core-note" style="border:none;padding:0;margin:0">simulation — not real time</span>',
      '      <button class="flow-replay" id="core-replay"><span class="tri">▶</span> Replay</button>',
      '    </div>',
      '    <div class="flow">',
      '      <div class="flow-guards">',
      '        <span class="flow-guard" data-order="0"><b>kill-switch</b> off? → stays silent</span>',
      '        <span class="flow-guard" data-order="1"><b>repeated prompt</b> (TTL)? → stays silent</span>',
      '      </div>',
      '      <div class="flow-row">',
      '        <div class="flow-stage" data-order="2" style="--sc:var(--core-blue)"><div class="fs-i">📝</div><div class="fs-t">prompt</div><div class="fs-s">your text</div></div>',
      '        <div class="flow-conn" data-order="3"></div>',
      '        <div class="flow-stage" data-order="4" style="--sc:var(--core-blue)"><div class="fs-i">✂️</div><div class="fs-t">tokenize</div><div class="fs-s">PT/EN stopwords · PT stemmer</div></div>',
      '      </div>',
      '      <div class="flow-merge-label" data-order="5">⬇ in parallel, over the same chunks</div>',
      '      <div class="flow-lanes">',
      '        <div class="flow-lane sem" data-order="6"><div class="fl-h"><span class="tag">semantic</span> embedding daemon (:47821) → <span class="core-mono">cosine</span></div><p>your prompt becomes a 384d vector; measures closeness of <b>meaning</b> to every learning.</p></div>',
      '        <div class="flow-lane lex" data-order="6"><div class="fl-h"><span class="tag">lexical</span> BM25 over the same chunks</div><p>catches the <b>exact term</b> — jargon, code, ticket numbers — right where the embedding slips.</p></div>',
      '        <div class="flow-lane branch" data-order="6"><div class="fl-h"><span class="tag">branch</span> ticket-additive block</div><p>if the session\'s branch (<span class="core-mono">/kb-mark</span>) matches a folder, the whole ticket comes along.</p></div>',
      '      </div>',
      '      <div class="flow-row">',
      '        <div class="flow-stage" data-order="7" style="--sc:var(--core-cyan)"><div class="fs-i">⚖️</div><div class="fs-t">hybrid</div><div class="fs-s">(0.7·cos + 0.3·bm25)×weights</div></div>',
      '        <div class="flow-conn" data-order="8"></div>',
      '        <div class="flow-stage" data-order="9" style="--sc:var(--green)"><div class="fs-i">🎚️</div><div class="fs-t">tiers</div><div class="fs-s">high/mid/low + GraphRAG 1-hop</div></div>',
      '      </div>',
      '      <div class="flow-out" data-order="10">',
      '        <div class="flow-out-head">✓ injected into your prompt</div>',
      '        <pre><span class="m">&lt;vault-context&gt;</span>\n## Top matches (tier=<span class="v">high</span>, via hybrid embedding (cosine+BM25)):\n- <span class="k">[[...order-idempotency-guard.md]]</span> (score=0.85)\n    "guard against duplicate order creation on the return flow…"\n## Related (GraphRAG 1-hop):\n- <span class="k">[[...unique-constraint-defense.md]]</span>\n<span class="m">&lt;/vault-context&gt;</span></pre>',
      '      </div>',
      '      <div class="flow-latency">',
      '        <span><b>daemon up:</b> hybrid cosine + BM25</span>',
      '        <span><b>daemon down:</b> pure BM25 (degrades, doesn\'t break)</span>',
      '      </div>',
      '    </div>',
      '  </div>',
      '</section>',

      // ===== 3. SYNC =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">the nightly routine</div>',
      '  <h2 class="core-h">Sync — distills knowledge from the work</h2>',
      '  <p class="core-lead"><b>The premise:</b> work leaves a trail in <b>two places</b> — <span class="hl">git</span> (what changed) and the <span class="hl">conversation with the agent</span> (why it changed). The sync reads both and <b>writes the doc for you</b>.</p></div>',
      '  <div class="timeline core-reveal">',
      '    <div class="tl-step"><div class="tl-t">fetch + prune <span class="when">01:00</span></div><p>updates every clone, discovers new branches and merges.</p></div>',
      '    <div class="tl-step"><div class="tl-t">capture</div><p>for each branch, takes your <b>authored</b> commits not yet inside the integration branches (<span class="core-mono">--not dev/master</span>) and joins <b>the git diff + the session transcripts</b>. The code yields the <b>what</b>; the conversation yields the <b>why</b> — intent, decisions and instructions that never show up in the diff and are usually the strongest learning. <span class="core-mono">experimental</span> branches are skipped.</p></div>',
      '    <div class="tl-step"><div class="tl-t">reconcile <span class="when">dedup · refute</span></div><p>no duplicate stacking: every learning that <b>already exists</b> is audited against the diff and classified — <b style="color:var(--green)">CONFIRM</b> (keep) · <b style="color:#6f9bff">ADJUST</b> (fold in nuance) · <b style="color:var(--core-cyan)">ADD</b> (new pattern) · <b style="color:var(--core-pink)">REFUTE</b> (contradicted → fixes the file + <span class="core-mono">## Correction history</span>).</p></div>',
      '    <div class="tl-step"><div class="tl-t">backfill</div><p>a branch that <b>committed and merged within the same window</b> would be invisible — mines <span class="core-mono">hwm..branch</span> with a per-run cap so nothing is lost and nothing floods.</p></div>',
      '    <div class="tl-step"><div class="tl-t">finalize</div><p>detects the merge to production in 3 layers (ancestry → <span class="core-mono">cherry</span> patch-id → combined squash) and flips the ticket to <span class="core-mono">resolved</span>.</p></div>',
      '    <div class="tl-step"><div class="tl-t">reindex</div><p>notifies the daemon (<span class="core-mono">{op:reindex}</span>) — the in-RAM vectors now include what was just learned.</p></div>',
      '  </div>',
      '  <p class="core-note">Attribution is deterministic: only authored commits <b>not reachable</b> from the integration branches count, and the diff runs against the nearest integration — a branch cut from <span class="core-mono">dev</span> compares against <span class="core-mono">dev</span>, not a stale <span class="core-mono">master</span>.</p>',
      '</section>',

      // ===== 4. EMBEDDINGS =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">meaning</div>',
      '  <h2 class="core-h">Embeddings — search by meaning, not by word</h2>',
      '  <p class="core-lead">Each learning becomes a <span class="hl">384-dimension vector</span>. Closeness in that space = closeness in meaning. "<b>token read as expired one second early</b>" lands right next to "<b>clock-skew tolerance between auth nodes</b>" without sharing a single word.</p></div>',
      '  <div class="embed-stage core-reveal">',
      '    <div class="scatter" id="core-scatter"><div class="scatter-grid"></div></div>',
      '    <div class="embed-side">',
      '      <h4>What it does</h4>',
      '      <p>Text → a point in a space where distance is difference in meaning. The query lights up and its semantic neighbors cluster around it.</p>',
      '      <h4>Why <span class="core-mono">paraphrase-multilingual-MiniLM-L12-v2</span></h4>',
      '      <ul class="embed-why">',
      '        <li><span class="mk">i18n</span><span><b>Multilingual:</b> vault and queries can be in any language — the model matches the <i>meaning</i> even across languages (a PT query finds an EN learning, and vice versa).</span></li>',
      '        <li><span class="mk">RAM</span><span><b>Small and fast:</b> runs locally (ONNX/fastembed), no API key, your code never leaves the machine.</span></li>',
      '        <li><span class="mk">384d</span><span><b>Good enough:</b> lean dimensionality = light index, low latency, fits in memory.</span></li>',
      '      </ul>',
      '      <p class="core-note" style="margin-top:14px">A <b>daemon</b> keeps model + vectors in RAM (loopback :47821), so no query ever pays the model-reload cost.</p>',
      '    </div>',
      '  </div>',
      '</section>',

      // ===== 5. BM25 =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">the literal</div>',
      '  <h2 class="core-h">BM25 — catches the exact term</h2>',
      '  <p class="core-lead">BM25 is the classic lexical ranking (Okapi): <b>saturated</b> term frequency (k1=1.5) × rarity (IDF) ÷ doc length (b=0.75). It nails exactly what embeddings miss: <span class="hl">rare jargon, codes, literal numbers</span>.</p></div>',
      '  <div class="core-reveal">',
      '    <div class="bm-query"><span class="ql">query:</span><span class="bm-term">order</span><span class="bm-term">return</span><span class="bm-term rare">java:S135</span></div>',
      '    <div class="bm-demo">',
      '      <div class="bm-doc"><div class="bd-h"><span>learning A</span><span class="bd-score">score 8.4</span></div><p>Guard against duplicate inbound <span class="bm-hit">order</span> creation on the <span class="bm-hit">return</span> flow; rule <span class="bm-hit bm-rare">java:S135</span> about multiple breaks.</p></div>',
      '      <div class="bm-doc"><div class="bd-h"><span>learning B</span><span class="bd-score">score 2.1</span></div><p>Carrier freight calculation; no direct relation to <span class="bm-hit">return</span> <span class="bm-hit">orders</span> nor to the cited rule.</p></div>',
      '    </div>',
      '  </div>',
      '  <p class="core-p core-reveal"><b>Why both together?</b> Embeddings understand <i>meaning</i> but slip on <span class="bm-rare" style="padding:1px 5px;border-radius:4px">java:S135</span> or <span class="bm-rare" style="padding:1px 5px;border-radius:4px">40579</span>. BM25 anchors on the literal. The hybrid adds them up — α=0.7 leans into meaning, β=0.3 holds onto the term.</p>',
      '</section>',

      // ===== 6. HYBRID + TIERS =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">how it all combines</div>',
      '  <h2 class="core-h">Hybrid score, weights and tiers</h2>',
      '  <p class="core-lead">The two signals become a single number, adjusted by <b>how general</b> the knowledge is and <b>how trustworthy</b> the ticket is. The top-1\'s tier decides how much gets injected: <b>high</b> carries the body excerpt, <b>mid</b> links only.</p></div>',
      '  <div class="formula-box core-reveal">',
      '    <div class="formula"><span class="term t-scope">scope</span><span class="op">×</span><span class="term t-status">status</span><span class="op">×</span>(<span class="op"> </span><span class="term t-cos">0.7 · cosine</span><span class="op">+</span><span class="term t-bm">0.3 · BM25ₙ</span><span class="op"> </span>)</div>',
      '    <div class="formula-legend">',
      '      <span><b style="color:var(--core-violet)">cosine</b> — closeness of meaning</span>',
      '      <span><b style="color:var(--core-cyan)">BM25ₙ</b> — literal match, normalized 0–1</span>',
      '      <span><b style="color:#6f9bff">scope</b> — how general the knowledge is</span>',
      '      <span><b style="color:var(--core-amber)">status</b> — how alive the ticket is</span>',
      '    </div>',
      '    <div class="weights">',
      '      <span class="wchip">workspace <span class="x">×1.30</span></span>',
      '      <span class="wchip">project <span class="x">×1.20</span></span>',
      '      <span class="wchip">ticket <span class="x">×1.00</span></span>',
      '      <span class="wchip dim">experimental <span class="x">×0.4</span></span>',
      '      <span class="wchip dead">discarded ×0.0</span>',
      '    </div>',
      '  </div>',
      '  <div class="tiers core-reveal" id="core-tiers">',
      '    <div class="tier high" style="--w:1"><span class="tl">high</span><span class="tbar"><i></i></span><span class="tr">score ≥ 0.70 · injects + top-1 body excerpt</span></div>',
      '    <div class="tier mid" style="--w:0.6"><span class="tl">mid</span><span class="tbar"><i></i></span><span class="tr">0.45 – 0.70 · injects links</span></div>',
      '    <div class="tier low" style="--w:0.25"><span class="tl">low</span><span class="tbar"><i></i></span><span class="tr">&lt; 0.45 · left out</span></div>',
      '  </div>',
      '  <p class="core-note">Daemon down? Falls back to pure BM25 with absolute-score tiers (high 8.0 / mid 5.0). <b>GraphRAG 1-hop</b>: the top 2 pull in neighbors via <span class="core-mono">[[wikilinks]]</span> between learnings.</p>',
      '</section>',

      // ===== 7. STATUSLINE =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">the status bar</div>',
      '  <h2 class="core-h">Statusline — KB always in sight</h2>',
      '  <p class="core-lead">One line sums up KB\'s state in your session. The data is <span class="hl">captured by retrieval itself</span> — no extra hook: every prompt updates a per-session state file.</p></div>',
      '  <div class="sl-mock core-reveal">',
      '    <div class="sl-bar core-mono">',
      '      <span class="sl-seg punct">[</span><span class="sl-seg">KB </span>',
      '      <span class="sl-seg health">✓</span><span class="sl-seg punct"> </span>',
      '      <span class="sl-seg branch">B:feat/recall*</span><span class="sl-seg punct"> </span>',
      '      <span class="sl-seg tier">H</span><span class="sl-seg punct"> </span>',
      '      <span class="sl-seg hits">7/9</span><span class="sl-seg punct">]</span>',
      '    </div>',
      '    <div class="sl-legend">',
      '      <div class="sl-leg"><div class="k"><span class="swatch" style="background:var(--green)"></span>health</div><p>✓ daemon up · ⚠ BM25 fallback · ✗ hooks off.</p></div>',
      '      <div class="sl-leg"><div class="k"><span class="swatch" style="background:var(--core-violet)"></span>B:branch*</div><p>the session\'s mark (<span class="core-mono">/kb-mark</span>); <b>*</b> = manual.</p></div>',
      '      <div class="sl-leg"><div class="k"><span class="swatch" style="background:var(--core-cyan)"></span>tier</div><p>H/M/L of the last injected <span class="core-mono">&lt;vault-context&gt;</span>.</p></div>',
      '      <div class="sl-leg"><div class="k"><span class="swatch" style="background:var(--core-amber)"></span>hits/total</div><p>prompts with tier ≥ mid ÷ total = hit rate.</p></div>',
      '    </div>',
      '    <p class="core-note">"KB actually used" has its own metric: when the body of an injected candidate is <b>opened with Read</b>, a PostToolUse tracker counts a <span class="core-mono">cited_read</span>. Citing <span class="core-mono">[[name]]</span> in the answer doesn\'t count — opening and reading does.</p>',
      '  </div>',
      '</section>',

      // ===== 8. PRINCIPLES =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">the guarantees</div>',
      '  <h2 class="core-h">Principles that hold it all together</h2></div>',
      '  <div class="principles core-reveal">',
      '    <div class="principle"><div class="pr-i">🔒</div><h4>Private by construction</h4><p>The vault is <b>local-only git by default</b>; the sync only ever commits, never pushes. Embeddings and BM25 run on your machine — recall talks to no API.</p></div>',
      '    <div class="principle"><div class="pr-i">🎯</div><h4>Deterministic</h4><p>Keys come from the payload. Never <code>"most recent / best guess"</code> — one wrong fallback would poison every future query.</p></div>',
      '    <div class="principle"><div class="pr-i">🪂</div><h4>Degrades gracefully</h4><p>Daemon down → pure BM25. Vault unresolved → stays silent. Every layer has a plan B; recall <b>never breaks</b> your prompt.</p></div>',
      '    <div class="principle"><div class="pr-i">🧱</div><h4>Agnostic</h4><p>OS (<code>schtasks/launchd/cron</code>), host (hooks or MCP via thin adapters), repo (zero hardcoded paths). Core + thin adapter.</p></div>',
      '  </div>',
      '</section>',

      '<div class="core-footer">KB · <b>model/OS-agnostic core</b> + a thin adapter per host · this page runs locally, just for you.</div>',
      '</div>'
    ].join("\n");
  }

  // ---- injection --------------------------------------------------------
  function inject() {
    if (document.getElementById("view-core")) return; // idempotent

    if (!document.querySelector('link[href="/static/core.css"]')) {
      var link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = "/static/core.css";
      document.head.appendChild(link);
    }

    var tabs = document.querySelector(".tabs");
    var wrap = document.querySelector(".wrap");
    if (!tabs || !wrap) return;

    var btn = document.createElement("button");
    btn.className = "tab";
    btn.setAttribute("data-view", "core");
    btn.textContent = "Core";
    tabs.appendChild(btn);

    var view = document.createElement("div");
    view.id = "view-core";
    view.className = "view hidden";
    view.innerHTML = panelHTML();
    // The page is EN; browsers offer to machine-translate it to the user's
    // language. Identifiers, commands and example output are copy-pasteable
    // contracts — shield them so translation can't mangle them.
    view.querySelectorAll("pre, code, .core-mono, .sl-bar, .formula").forEach(function (el) {
      el.setAttribute("translate", "no");
    });
    var foot = wrap.querySelector(".foot");
    wrap.insertBefore(view, foot || null);

    // Delegated nav handling. app.js owns config/knowledge; we own core.
    tabs.addEventListener("click", function (e) {
      var t = e.target.closest(".tab");
      if (!t) return;
      var isCore = t.getAttribute("data-view") === "core";
      view.classList.toggle("hidden", !isCore);
      if (isCore) {
        // app.js has no listener on our injected tab — do the full switch here.
        var views = document.querySelectorAll(".view");
        for (var i = 0; i < views.length; i++) {
          if (views[i].id !== "view-core") views[i].classList.add("hidden");
        }
        var allTabs = document.querySelectorAll(".tab");
        for (var j = 0; j < allTabs.length; j++) allTabs[j].classList.toggle("is-active", allTabs[j] === t);
        window.scrollTo({ top: 0, behavior: REDUCED ? "auto" : "smooth" });
        onShown();
      }
    });

    wireFlow(view);   // before wireReveals: the reduced-motion path lights the flow
    wireReveals(view);
    enrichLive();
  }

  // ---- scroll reveal (one-shot) + section triggers ----------------------
  var _shownOnce = false;
  function wireReveals(root) {
    var nodes = root.querySelectorAll(".core-reveal:not(.is-visible)");
    if (!("IntersectionObserver" in window) || REDUCED) {
      for (var i = 0; i < nodes.length; i++) nodes[i].classList.add("is-visible");
      triggerScatter(root); triggerTiers(root); lightFlowAll();
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        en.target.classList.add("is-visible");
        io.unobserve(en.target);
        if (en.target.querySelector && en.target.querySelector("#core-scatter")) triggerScatter(root);
        if (en.target.querySelector && en.target.querySelector("#core-replay")) maybePlayFlow();
        if (en.target.id === "core-tiers" || (en.target.querySelector && en.target.querySelector("#core-tiers"))) triggerTiers(root);
      });
    }, { threshold: 0.18, rootMargin: "0px 0px -8% 0px" });
    for (var k = 0; k < nodes.length; k++) io.observe(nodes[k]);
  }

  // called the first time the Core tab is opened (reveals fire on scroll, but
  // the panel may already be in view, so nudge the first batch + animations)
  function onShown() {
    if (_shownOnce) { return; }
    _shownOnce = true;
    var view = document.getElementById("view-core");
    // Reveal only what's already above the fold. The IntersectionObserver drives
    // the section animations (pipeline, scatter, tiers) as they scroll into view,
    // so they fire on-screen on the first pass instead of off-screen on open.
    var nodes = view.querySelectorAll(".core-reveal");
    nodes.forEach(function (n) {
      var r = n.getBoundingClientRect();
      if (r.top < window.innerHeight * 0.9) n.classList.add("is-visible");
    });
  }

  // ---- embedding scatter ------------------------------------------------
  var SCATTER = [
    // [startX%, startY%, finalX%, finalY%, class]
    [50, 50, 50, 48, "q"],
    [12, 80, 40, 40, "c2 near"], [88, 18, 60, 38, "c2 near"], [20, 22, 44, 58, "c2 near"], [80, 78, 58, 60, "c2 near"],
    [70, 12, 38, 30, "c1 near"], [30, 88, 64, 30, "c1"],
    [10, 50, 16, 22, "c1"], [90, 50, 86, 74, "c3"], [50, 10, 24, 80, "c3"], [50, 90, 80, 18, "c1"],
    [15, 35, 12, 70, "c3"], [85, 35, 88, 28, "c2"], [35, 15, 30, 18, "c1"], [65, 85, 70, 82, "c3"]
  ];
  function triggerScatter(root) {
    var sc = (root || document).querySelector("#core-scatter");
    if (!sc || sc._built) return;
    sc._built = true;
    SCATTER.forEach(function (p) {
      var d = document.createElement("div");
      d.className = "pt " + p[4];
      d.style.setProperty("--sx", p[0] + "%");
      d.style.setProperty("--sy", p[1] + "%");
      d.style.setProperty("--x", p[2] + "%");
      d.style.setProperty("--y", p[3] + "%");
      sc.appendChild(d);
    });
    requestAnimationFrame(function () { setTimeout(function () { sc.classList.add("go"); }, REDUCED ? 0 : 120); });
  }

  function triggerTiers(root) {
    var t = (root || document).querySelector("#core-tiers");
    if (t) t.classList.add("go");
  }

  // ---- pipeline walker --------------------------------------------------
  var _flowEls = null, _playing = false, _flowPlayed = false;
  function maybePlayFlow() {
    if (_flowPlayed) return;          // first scroll-into-view only; button replays anytime
    _flowPlayed = true;
    if (REDUCED) lightFlowAll(); else playFlow();
  }
  function wireFlow(root) {
    _flowEls = Array.prototype.slice.call(root.querySelectorAll("[data-order]"));
    _flowEls.sort(function (a, b) { return (+a.getAttribute("data-order")) - (+b.getAttribute("data-order")); });
    var btn = root.querySelector("#core-replay");
    if (btn) btn.addEventListener("click", function () { REDUCED ? lightFlowAll() : playFlow(); });
  }
  function clearFlow() {
    if (!_flowEls) return;
    _flowEls.forEach(function (el) { el.classList.remove("lit"); });
  }
  function lightFlowAll() { clearFlow(); if (_flowEls) _flowEls.forEach(function (el) { el.classList.add("lit"); }); }
  function playFlow() {
    if (!_flowEls || _playing) return;
    _playing = true; clearFlow();
    // group by data-order so parallel lanes (same order) light together
    var groups = {};
    _flowEls.forEach(function (el) { var o = el.getAttribute("data-order"); (groups[o] = groups[o] || []).push(el); });
    var orders = Object.keys(groups).map(Number).sort(function (a, b) { return a - b; });
    var i = 0;
    (function step() {
      if (i >= orders.length) { _playing = false; return; }
      groups[orders[i]].forEach(function (el) { el.classList.add("lit"); });
      i++;
      setTimeout(step, 360);
    })();
  }

  // ---- progressive live enrichment (never load-bearing) -----------------
  function enrichLive() {
    if (!TOKEN) return;
    api("/api/knowledge/overview").then(function (ov) {
      if (ov && ov.totals) {
        setText("cs-learnings", ov.totals.learnings);
        setText("cs-tickets", ov.totals.tickets);
      }
    }).catch(function () {});
    api("/api/status").then(function (st) {
      var d = (st && st.daemon) || {};
      var el = document.getElementById("cs-daemon");
      if (!el) return;
      if (d.up && d.model_loaded) el.innerHTML = '<span class="ok">● in RAM</span>';
      else if (d.up) el.innerHTML = '<span class="warn">● loading</span>';
      else el.innerHTML = '<span class="bad">● BM25</span>';
    }).catch(function () {});
    api("/api/sync-history").then(function (r) {
      var runs = (r && r.runs) || [];
      var el = document.getElementById("cs-sync");
      if (!el) return;
      if (!runs.length) { el.textContent = "not yet"; return; }
      el.textContent = (runs[0].ts || "").slice(0, 10) || "—";
    }).catch(function () {});
  }
  function setText(id, v) { var e = document.getElementById(id); if (e) e.textContent = v; }

  // ---- boot -------------------------------------------------------------
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", inject);
  else inject();
})();
