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
 *   Haiku tiers: high >= 0.75, mid 0.45-0.75, low < 0.45
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
      '    <span class="core-eyebrow"><span class="dotpulse"></span> o núcleo do KB, de ponta a ponta</span>',
      '    <h1 class="core-title">Seu conhecimento de engenharia,<br><span class="grad">capturado e devolvido</span> sozinho.</h1>',
      '    <p class="core-thesis">Resolver um ticket gera conhecimento que normalmente <b>evapora</b> — sai da sua cabeça, sai da empresa, vira re-investigação. O KB destila esse conhecimento <b>do git e da sua conversa com o agente</b> e o injeta de volta <b>no momento exato</b> em que você pergunta algo relacionado. Sem você escrever doc. Sem você lembrar de buscar.</p>',
      '    <div class="core-livebar">',
      '      <div class="core-stat"><div class="n" id="cs-learnings">—</div><div class="l">learnings no vault</div></div>',
      '      <div class="core-stat"><div class="n" id="cs-tickets">—</div><div class="l">tickets</div></div>',
      '      <div class="core-stat"><div class="n small" id="cs-daemon">—</div><div class="l">índice em RAM</div></div>',
      '      <div class="core-stat"><div class="n small" id="cs-sync">—</div><div class="l">última sync</div></div>',
      '    </div>',
      '  </div>',
      '</section>',

      // ===== 1. OBJETIVO =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">o objetivo</div>',
      '  <h2 class="core-h">Uma memória de engenharia que se mantém sozinha</h2>',
      '  <p class="core-lead">O alvo não é "mais um wiki" que ninguém atualiza. É um <span class="hl">loop fechado</span>: você trabalha, o KB aprende do que ficou no git, e na próxima pergunta o aprendizado volta pra você. Três peças fazem isso acontecer.</p></div>',
      '  <div class="core-loop core-reveal">',
      '    <div class="core-loop-ring"></div>',
      '    <div class="core-orbit-dot"></div>',
      '    <div class="core-loop-center"><span class="lc-a">trabalho</span><span class="lc-arrow">↓</span><span class="lc-b">memória</span></div>',
      '    <div class="core-node" style="--x:0px;--y:-128px"><div class="cn-i">⌨️</div><div class="cn-t">Trabalho</div><div class="cn-s">commits + conversa</div></div>',
      '    <div class="core-node" style="--x:128px;--y:0px"><div class="cn-i">🌙</div><div class="cn-t">Captura</div><div class="cn-s">git + conversa</div></div>',
      '    <div class="core-node" style="--x:0px;--y:128px"><div class="cn-i">🧩</div><div class="cn-t">Índice</div><div class="cn-s">embeddings + BM25</div></div>',
      '    <div class="core-node" style="--x:-128px;--y:0px"><div class="cn-i">⚡</div><div class="cn-t">Recall</div><div class="cn-s">injeta no prompt</div></div>',
      '  </div>',
      '  <div class="core-grid2 core-reveal">',
      '    <div class="pillar" style="--c:var(--core-violet)"><div class="p-i">🌙</div><h4>Sync</h4><p>Toda noite lê o git <b>e a conversa com o agente</b> e <b>destila learnings</b> do trabalho que landou. Você não documenta — ele documenta por você.</p><span class="p-tag">git + conversa → markdown</span></div>',
      '    <div class="pillar" style="--c:var(--core-cyan)"><div class="p-i">🧩</div><h4>Índice</h4><p>Mantém o vault em RAM como <b>vetores + termos</b>. Semântica e literal, prontos pra busca em milissegundos.</p><span class="p-tag">embeddings + BM25</span></div>',
      '    <div class="pillar" style="--c:var(--core-blue)"><div class="p-i">⚡</div><h4>Recall</h4><p>A cada prompt, busca o que é relevante e <b>injeta no contexto</b> antes do LLM responder. Zero clique.</p><span class="p-tag">&lt;vault-context&gt;</span></div>',
      '  </div>',
      '</section>',

      // ===== 2. PIPELINE (centerpiece) =====
      '<section class="core-section" id="core-flow-sec">',
      '  <div class="core-reveal"><div class="core-kicker">o que acontece quando você envia um prompt</div>',
      '  <h2 class="core-h">O fluxo do recall, passo a passo</h2>',
      '  <p class="core-lead">Antes do LLM ver uma palavra, um hook (<span class="hl">UserPromptSubmit</span>) intercepta seu prompt e roda esta pipeline. Tudo local, em milissegundos — só o rerank é uma chamada de API, e mesmo ela tem plano B.</p></div>',
      '  <div class="flow-wrap core-reveal">',
      '    <div class="flow-head">',
      '      <span class="core-note" style="border:none;padding:0;margin:0">simulação — não é tempo real</span>',
      '      <button class="flow-replay" id="core-replay"><span class="tri">▶</span> Reproduzir</button>',
      '    </div>',
      '    <div class="flow">',
      '      <div class="flow-guards">',
      '        <span class="flow-guard" data-order="0"><b>kill-switch</b> desligado? → silencia</span>',
      '        <span class="flow-guard" data-order="1"><b>prompt repetido</b> (TTL)? → silencia</span>',
      '      </div>',
      '      <div class="flow-row">',
      '        <div class="flow-stage" data-order="2" style="--sc:var(--core-blue)"><div class="fs-i">📝</div><div class="fs-t">prompt</div><div class="fs-s">seu texto</div></div>',
      '        <div class="flow-conn" data-order="3"></div>',
      '        <div class="flow-stage" data-order="4" style="--sc:var(--core-blue)"><div class="fs-i">✂️</div><div class="fs-t">tokeniza</div><div class="fs-s">stopwords PT/EN · stemmer PT</div></div>',
      '      </div>',
      '      <div class="flow-merge-label" data-order="5">⬇ em paralelo, sobre os mesmos chunks</div>',
      '      <div class="flow-lanes">',
      '        <div class="flow-lane sem" data-order="6"><div class="fl-h"><span class="tag">semântica</span> embedding daemon (:47821) → <span class="core-mono">cosine</span></div><p>seu prompt vira um vetor 384d; mede proximidade de <b>significado</b> com cada learning.</p></div>',
      '        <div class="flow-lane lex" data-order="6"><div class="fl-h"><span class="tag">léxica</span> BM25 sobre os mesmos chunks</div><p>acha o <b>termo exato</b> — jargão, código, número de ticket — onde o embedding patina.</p></div>',
      '        <div class="flow-lane branch" data-order="6"><div class="fl-h"><span class="tag">branch</span> bloco ticket-additive</div><p>se a branch da sessão (<span class="core-mono">/kb-mark</span>) casa uma pasta, puxa o ticket inteiro junto.</p></div>',
      '      </div>',
      '      <div class="flow-row">',
      '        <div class="flow-stage" data-order="7" style="--sc:var(--core-cyan)"><div class="fs-i">⚖️</div><div class="fs-t">híbrido</div><div class="fs-s">(0.7·cos + 0.3·bm25)×pesos</div></div>',
      '        <div class="flow-conn" data-order="8"></div>',
      '        <div class="flow-stage" data-order="9" style="--sc:var(--core-violet)"><div class="fs-i">🧠</div><div class="fs-t">rerank</div><div class="fs-s">Haiku top 5 · API → cai p/ BM25</div></div>',
      '        <div class="flow-conn" data-order="10"></div>',
      '        <div class="flow-stage" data-order="11" style="--sc:var(--green)"><div class="fs-i">🎚️</div><div class="fs-t">tiers</div><div class="fs-s">high/mid/low + GraphRAG 1-hop</div></div>',
      '      </div>',
      '      <div class="flow-out" data-order="12">',
      '        <div class="flow-out-head">✓ injetado no seu prompt</div>',
      '        <pre><span class="m">&lt;vault-context&gt;</span>\n## Top matches (tier=<span class="v">high</span>, via Haiku rerank):\n- <span class="k">[[...idempotencia-gerar-pedido.md]]</span> (conf=0.85)\n    "guard contra duplicidade ao gerar pedido…"\n## Related (GraphRAG 1-hop):\n- <span class="k">[[...unique-constraint-defense.md]]</span>\n<span class="m">&lt;/vault-context&gt;</span></pre>',
      '      </div>',
      '      <div class="flow-latency">',
      '        <span><b>daemon up:</b> cosine + BM25 híbrido</span>',
      '        <span><b>daemon down:</b> BM25 puro (degrada, não quebra)</span>',
      '        <span><b>API down:</b> tiers por score BM25</span>',
      '      </div>',
      '    </div>',
      '  </div>',
      '</section>',

      // ===== 3. SYNC =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">a rotina noturna</div>',
      '  <h2 class="core-h">Sync — destila o conhecimento do trabalho</h2>',
      '  <p class="core-lead"><b>A premissa:</b> o trabalho deixa rastro em <b>dois lugares</b> — o <span class="hl">git</span> (o que mudou) e a <span class="hl">conversa com o agente</span> (por que mudou). O sync lê os dois e <b>escreve a doc por você</b>.</p></div>',
      '  <div class="timeline core-reveal">',
      '    <div class="tl-step"><div class="tl-t">fetch + prune <span class="when">01:00</span></div><p>atualiza todos os clones, descobre branches novas e merges.</p></div>',
      '    <div class="tl-step"><div class="tl-t">capture</div><p>pra cada branch, pega os commits <b>autorais</b> ainda fora das integration branches (<span class="core-mono">--not dev/master</span>) e junta <b>o diff do git + os transcripts da sessão</b>. Do código tira o <b>o quê</b>; da conversa tira o <b>porquê</b> — sua intenção, decisão e instrução que não aparecem no diff e costumam ser o aprendizado mais forte. Branch <span class="core-mono">experimental</span> é pulada.</p></div>',
      '    <div class="tl-step"><div class="tl-t">concilia <span class="when">dedup · refuta</span></div><p>não empilha duplicata: audita cada learning que <b>já existe</b> contra o diff e classifica — <b style="color:var(--green)">CONFIRMA</b> (mantém) · <b style="color:#6f9bff">AJUSTA</b> (incorpora nuance) · <b style="color:var(--core-cyan)">ADICIONA</b> (padrão novo) · <b style="color:var(--core-pink)">REFUTA</b> (contradiz → corrige o arquivo + <span class="core-mono">## Correction history</span>).</p></div>',
      '    <div class="tl-step"><div class="tl-t">backfill</div><p>branch que <b>commitou e mergeou no mesmo intervalo</b> seria invisível — minera <span class="core-mono">hwm..branch</span> com teto por run pra não perder nem floodar.</p></div>',
      '    <div class="tl-step"><div class="tl-t">finalize</div><p>detecta merge pra produção em 3 camadas (ancestry → <span class="core-mono">cherry</span> patch-id → squash combinado) e marca o ticket <span class="core-mono">resolved</span>.</p></div>',
      '    <div class="tl-step"><div class="tl-t">reindex</div><p>avisa o daemon (<span class="core-mono">{op:reindex}</span>) — os vetores em RAM passam a incluir o que acabou de ser aprendido.</p></div>',
      '  </div>',
      '  <p class="core-note">Atribuição é determinística: só conta commit autoral <b>não alcançável</b> das integration branches, e faz o diff contra a integration mais próxima — branch criada de <span class="core-mono">dev</span> compara com <span class="core-mono">dev</span>, não com um <span class="core-mono">master</span> velho.</p>',
      '</section>',

      // ===== 4. EMBEDDINGS =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">significado</div>',
      '  <h2 class="core-h">Embeddings — busca por sentido, não por palavra</h2>',
      '  <p class="core-lead">Cada learning vira um <span class="hl">vetor de 384 dimensões</span>. Proximidade no espaço = proximidade de significado. "<b>devolução gera pedido de entrada</b>" fica perto de "<b>estorno de nota</b>" mesmo sem dividir uma palavra.</p></div>',
      '  <div class="embed-stage core-reveal">',
      '    <div class="scatter" id="core-scatter"><div class="scatter-grid"></div></div>',
      '    <div class="embed-side">',
      '      <h4>O que faz</h4>',
      '      <p>Texto → ponto num espaço onde a distância é a diferença de sentido. A query acende e os vizinhos semânticos se agrupam ao redor dela.</p>',
      '      <h4>Por que <span class="core-mono">paraphrase-multilingual-MiniLM-L12-v2</span></h4>',
      '      <ul class="embed-why">',
      '        <li><span class="mk">i18n</span><span><b>Multilingual:</b> vault e queries podem estar em qualquer idioma — o modelo casa o <i>sentido</i> mesmo entre línguas (query em PT acha learning em EN, e vice-versa).</span></li>',
      '        <li><span class="mk">RAM</span><span><b>Pequeno e rápido:</b> roda local (ONNX/fastembed), sem API key, sem mandar seu código pra ninguém.</span></li>',
      '        <li><span class="mk">384d</span><span><b>Bom o bastante:</b> dimensão enxuta = índice leve, latência baixa, cabe em memória.</span></li>',
      '      </ul>',
      '      <p class="core-note" style="margin-top:14px">Um <b>daemon</b> mantém modelo + vetores em RAM (loopback :47821), então nenhuma query paga o custo de recarregar o modelo.</p>',
      '    </div>',
      '  </div>',
      '</section>',

      // ===== 5. BM25 =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">o literal</div>',
      '  <h2 class="core-h">BM25 — pega o termo exato</h2>',
      '  <p class="core-lead">BM25 é o ranking lexical clássico (Okapi): frequência do termo <b>saturada</b> (k1=1.5) × raridade (IDF) ÷ tamanho do doc (b=0.75). Ele acerta justo onde o embedding erra: <span class="hl">jargão raro, códigos, números literais</span>.</p></div>',
      '  <div class="core-reveal">',
      '    <div class="bm-query"><span class="ql">query:</span><span class="bm-term">pedido</span><span class="bm-term">devolução</span><span class="bm-term rare">java:S135</span></div>',
      '    <div class="bm-demo">',
      '      <div class="bm-doc"><div class="bd-h"><span>learning A</span><span class="bd-score">score 8.4</span></div><p>Guard contra duplicidade ao gerar <span class="bm-hit">pedido</span> de entrada na <span class="bm-hit">devolução</span>; regra <span class="bm-hit bm-rare">java:S135</span> sobre múltiplos break.</p></div>',
      '      <div class="bm-doc"><div class="bd-h"><span>learning B</span><span class="bd-score">score 2.1</span></div><p>Cálculo de frete por transportadora; sem relação direta com <span class="bm-hit">pedido</span> de devolução nem com a regra citada.</p></div>',
      '    </div>',
      '  </div>',
      '  <p class="core-p core-reveal"><b>Por que os dois juntos?</b> Embedding entende <i>significado</i> mas escorrega em <span class="bm-rare" style="padding:1px 5px;border-radius:4px">java:S135</span> ou <span class="bm-rare" style="padding:1px 5px;border-radius:4px">40579</span>. BM25 ancora no literal. O híbrido soma os dois — α=0.7 puxa pro sentido, β=0.3 segura no termo.</p>',
      '</section>',

      // ===== 6. HÍBRIDO + TIERS =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">como tudo se combina</div>',
      '  <h2 class="core-h">Score híbrido, pesos e tiers</h2>',
      '  <p class="core-lead">As duas pistas viram um número só, ajustado por <b>quão geral</b> é o conhecimento e <b>quão confiável</b> é o ticket. Depois o Haiku reordena os finalistas e decide quanto injeta.</p></div>',
      '  <div class="formula-box core-reveal">',
      '    <div class="formula"><span class="term t-scope">scope</span><span class="op">×</span><span class="term t-status">status</span><span class="op">×</span>(<span class="op"> </span><span class="term t-cos">0.7 · cosine</span><span class="op">+</span><span class="term t-bm">0.3 · BM25ₙ</span><span class="op"> </span>)</div>',
      '    <div class="formula-legend">',
      '      <span><b style="color:var(--core-violet)">cosine</b> — proximidade de significado</span>',
      '      <span><b style="color:var(--core-cyan)">BM25ₙ</b> — match literal, normalizado 0–1</span>',
      '      <span><b style="color:#6f9bff">scope</b> — quão geral é o saber</span>',
      '      <span><b style="color:var(--core-amber)">status</b> — quão vivo é o ticket</span>',
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
      '    <div class="tier high" style="--w:1"><span class="tl">high</span><span class="tbar"><i></i></span><span class="tr">conf ≥ 0.75 · injeta forte</span></div>',
      '    <div class="tier mid" style="--w:0.6"><span class="tl">mid</span><span class="tbar"><i></i></span><span class="tr">0.45 – 0.75 · injeta</span></div>',
      '    <div class="tier low" style="--w:0.25"><span class="tl">low</span><span class="tbar"><i></i></span><span class="tr">&lt; 0.45 · fica de fora</span></div>',
      '  </div>',
      '  <p class="core-note">Daemon fora? cai pra BM25 puro com tiers por score absoluto (high 8.0 / mid 5.0). <b>GraphRAG 1-hop</b>: os 2 melhores puxam vizinhos via <span class="core-mono">[[wikilinks]]</span> entre os learnings.</p>',
      '</section>',

      // ===== 7. STATUSLINE =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">a barra de status</div>',
      '  <h2 class="core-h">Statusline — o KB sempre à vista</h2>',
      '  <p class="core-lead">Uma linha resume o estado do KB na sua sessão. As informações são <span class="hl">capturadas pelo próprio retrieval</span> — sem hook extra: cada prompt atualiza um arquivo de estado por sessão.</p></div>',
      '  <div class="sl-mock core-reveal">',
      '    <div class="sl-bar core-mono">',
      '      <span class="sl-seg punct">[</span><span class="sl-seg">KB </span>',
      '      <span class="sl-seg health">✓</span><span class="sl-seg punct"> </span>',
      '      <span class="sl-seg branch">B:feat/recall*</span><span class="sl-seg punct"> </span>',
      '      <span class="sl-seg tier">H</span><span class="sl-seg punct"> </span>',
      '      <span class="sl-seg hits">7/9</span><span class="sl-seg punct">]</span>',
      '    </div>',
      '    <div class="sl-legend">',
      '      <div class="sl-leg"><div class="k"><span class="swatch" style="background:var(--green)"></span>health</div><p>✓ daemon up · ⚠ BM25 fallback · ✗ hooks desligados.</p></div>',
      '      <div class="sl-leg"><div class="k"><span class="swatch" style="background:var(--core-violet)"></span>B:branch*</div><p>marca da sessão (<span class="core-mono">/kb-mark</span>); <b>*</b> = manual.</p></div>',
      '      <div class="sl-leg"><div class="k"><span class="swatch" style="background:var(--core-cyan)"></span>tier</div><p>H/M/L do último <span class="core-mono">&lt;vault-context&gt;</span> injetado.</p></div>',
      '      <div class="sl-leg"><div class="k"><span class="swatch" style="background:var(--core-amber)"></span>hits/total</div><p>prompts com tier ≥ mid ÷ total = taxa de acerto.</p></div>',
      '    </div>',
      '    <p class="core-note">"KB usado de verdade" tem métrica própria: quando você <b>abre com Read</b> o body de um candidato injetado, um track em PostToolUse conta esse <span class="core-mono">cited_read</span>. Citar <span class="core-mono">[[nome]]</span> na resposta não conta — abrir e ler conta.</p>',
      '  </div>',
      '</section>',

      // ===== 8. PRINCÍPIOS =====
      '<section class="core-section">',
      '  <div class="core-reveal"><div class="core-kicker">as garantias</div>',
      '  <h2 class="core-h">Princípios que seguram tudo</h2></div>',
      '  <div class="principles core-reveal">',
      '    <div class="principle"><div class="pr-i">🔒</div><h4>Privado por construção</h4><p>O vault é git <b>local, sem remoto, nunca</b>. Embeddings e BM25 rodam na sua máquina. Só o rerank opcional fala com API.</p></div>',
      '    <div class="principle"><div class="pr-i">🎯</div><h4>Determinístico</h4><p>Chave vem do payload. Nunca <code>"mais recente / melhor palpite"</code> — um fallback errado envenenaria consultas futuras.</p></div>',
      '    <div class="principle"><div class="pr-i">🪂</div><h4>Degrada com graça</h4><p>Daemon cai → BM25 puro. API cai → tiers por score. Cada camada tem plano B; o recall <b>nunca quebra</b> o prompt.</p></div>',
      '    <div class="principle"><div class="pr-i">🧱</div><h4>Agnóstico</h4><p>OS (<code>schtasks/launchd/cron</code>), modelo (Claude/Codex via adapter), repo (zero path hardcoded). Núcleo + adapter fino.</p></div>',
      '  </div>',
      '</section>',

      '<div class="core-footer">KB · <b>núcleo model/OS-agnóstico</b> + adapter fino por host · esta página roda local, só pra você.</div>',
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
      if (d.up && d.model_loaded) el.innerHTML = '<span class="ok">● em RAM</span>';
      else if (d.up) el.innerHTML = '<span class="warn">● carregando</span>';
      else el.innerHTML = '<span class="bad">● BM25</span>';
    }).catch(function () {});
    api("/api/sync-history").then(function (r) {
      var runs = (r && r.runs) || [];
      var el = document.getElementById("cs-sync");
      if (!el) return;
      if (!runs.length) { el.textContent = "ainda não"; return; }
      el.textContent = (runs[0].ts || "").slice(0, 10) || "—";
    }).catch(function () {});
  }
  function setText(id, v) { var e = document.getElementById(id); if (e) e.textContent = v; }

  // ---- boot -------------------------------------------------------------
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", inject);
  else inject();
})();
