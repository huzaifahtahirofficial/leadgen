/* Nestick control panel — vanilla JS, no build step. */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  // Backend API origin. Empty on the desktop/self-hosted app (same origin);
  // set to the Render service URL by config.js in the Vercel build.
  const API_BASE = (window.NESTICK_API_BASE || "").replace(/\/+$/, "");
  // JWT returned by /api/login (central auth). Persisted so reloads stay signed in.
  let token = "";
  try { token = localStorage.getItem("nestick.token") || ""; } catch {}
  const authHeaders = () => (token ? { Authorization: `Bearer ${token}` } : {});
  const form = $("#jobForm");
  const runBtn = $("#runBtn");
  const stopBtn = $("#stopBtn");
  const pill = $("#statusPill");
  const rows = $("#rows");
  const empty = $("#empty");
  const bar = $("#bar");
  const logOut = $("#logOut");

  let poll = null;
  let logIndex = 0;
  let leads = [];
  let sortKey = "score";
  let sortDir = -1;
  let hasFiles = false;
  let running = false;
  let me = null;                 // { plan, can_scrape, ... } from /api/me
  let loginMode = "signin";      // "signin" | "register" on the login gate

  /* ---------------- tabs ---------------- */
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const mode = tab.dataset.mode;
      $("#pane-search").classList.toggle("hidden", mode !== "search");
      $("#pane-urls").classList.toggle("hidden", mode !== "urls");
    });
  });

  /* ---------------- helpers ---------------- */
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function formData() {
    const d = {};
    new FormData(form).forEach((v, k) => { d[k] = v; });
    form.querySelectorAll('input[type=checkbox]').forEach((c) => { d[c.name] = c.checked; });
    return d;
  }

  function setRunning(on) {
    running = on;
    runBtn.disabled = on || applyLock();
    runBtn.textContent = on ? "Scraping…" : "Start scraping";
    stopBtn.classList.toggle("hidden", !on);
    bar.classList.toggle("indet", on);
    if (!on) bar.style.width = "0";
  }

  // A subscribed user (or auth-off legacy mode) may scrape; otherwise the API
  // would reject /api/start with 403, so the button is gated up front.
  function applyLock() {
    return !!(me && me.enabled && !me.can_scrape);
  }

  function setPill(cls, text) {
    pill.className = "pill " + cls;
    pill.textContent = text;
  }

  /* ---------------- rendering ---------------- */
  function scoreClass(n) { return n >= 60 ? "sc-hi" : n >= 30 ? "sc-md" : "sc-lo"; }

  function socialChips(socials) {
    const names = { linkedin: "in", twitter: "tw", facebook: "fb", instagram: "ig",
                    youtube: "yt", github: "gh", tiktok: "tt", telegram: "tg",
                    whatsapp: "wa", medium: "md" };
    return Object.entries(socials || {})
      .map(([k, urls]) =>
        `<a class="chip" href="${esc(urls[0])}" target="_blank" rel="noopener">${names[k] || k}</a>`)
      .join("");
  }

  function render() {
    const q = ($("#filter").value || "").toLowerCase();
    const onlyEmail = $("#onlyEmail").checked;

    let view = leads.filter((l) => {
      if (onlyEmail && !l.emails.length) return false;
      if (!q) return true;
      return (l.domain + " " + l.name + " " + l.emails.join(" ") + " " + l.phones.join(" "))
        .toLowerCase().includes(q);
    });

    view.sort((a, b) => {
      const x = a[sortKey], y = b[sortKey];
      if (typeof x === "number") return (x - y) * sortDir;
      return String(x).localeCompare(String(y)) * sortDir;
    });

    empty.classList.toggle("hidden", view.length > 0);
    rows.innerHTML = view.map((l) => `
      <tr>
        <td><span class="score-badge ${scoreClass(l.score)}">${l.score}</span></td>
        <td><a href="${esc(l.url || "#")}" target="_blank" rel="noopener">${esc(l.domain)}</a></td>
        <td>${esc(l.name).slice(0, 46) || '<span class="muted">—</span>'}</td>
        <td>${l.emails.length
              ? l.emails.slice(0, 4).map((e) =>
                  `<a class="mail" href="mailto:${esc(e)}">${esc(e)}</a>`).join("") +
                (l.emails.length > 4 ? `<span class="muted">+${l.emails.length - 4} more</span>` : "")
              : '<span class="muted">—</span>'}</td>
        <td>${l.phones.length
              ? l.phones.slice(0, 2).map((p) => `<span class="mail">${esc(p)}</span>`).join("")
              : '<span class="muted">—</span>'}</td>
        <td><div class="chips">${socialChips(l.socials) || '<span class="muted">—</span>'}</div></td>
      </tr>`).join("");
  }

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      sortDir = sortKey === k ? -sortDir : (k === "score" ? -1 : 1);
      sortKey = k;
      render();
    });
  });
  $("#filter").addEventListener("input", render);
  $("#onlyEmail").addEventListener("change", render);

  /* ---------------- polling ---------------- */
  let missedPolls = 0;
  let ticking = false;
  async function tick() {
    if (ticking) return;          // never overlap requests
    ticking = true;
    try { await tickOnce(); } finally { ticking = false; }
  }

  async function tickOnce() {
    let st;
    try {
      st = await api(`${API_BASE}/api/status?log=${logIndex}`, {}, 2);
      if (missedPolls) setOffline(false);
      missedPolls = 0;
    } catch (e) {
      // Two consecutive failures means the console window was probably closed.
      // A single slow poll is normal during a heavy crawl. Only declare the
      // server gone after several consecutive failures (~8 seconds).
      if (++missedPolls >= 6) setOffline(true);
      return;
    }

    const s = st.stats || {}, sm = st.summary || {};
    $("#s-leads").textContent  = sm.leads ?? 0;
    $("#s-emails").textContent = sm.unique_emails ?? 0;
    $("#s-phones").textContent = sm.with_phone ?? 0;
    $("#s-req").textContent    = s.requests ?? 0;
    $("#s-rate").textContent   = s.req_per_s ?? 0;
    $("#s-time").textContent   = (st.elapsed ?? 0) + "s";

    leads = st.leads || [];
    render();

    if (st.log && st.log.length) {
      logIndex = st.log_index;
      logOut.insertAdjacentHTML("beforeend",
        st.log.map((l) =>
          `<span class="lv-${l.level}">${esc(l.t)}  ${esc(l.msg)}</span>\n`).join(""));
      logOut.scrollTop = logOut.scrollHeight;
    }

    const banner = $("#apiBanner");
    if ((st.api_errors || []).length) {
      banner.innerHTML =
        "<strong>API problem</strong><ul>" +
        st.api_errors.map((e) => `<li>${esc(e)}</li>`).join("") +
        '</ul><p class="tip">The scrape continued using the keyless engine. ' +
        'Check your key under <em>API keys</em>.</p>';
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }

    renderAnalytics(st.analytics);

    hasFiles = (st.files || []).length > 0;
    document.querySelectorAll(".dl").forEach((a) => a.classList.toggle("off", !hasFiles));

    if (!st.running) {
      stopPolling();
      setRunning(false);
      if (st.error) setPill("error", "Error");
      else if (st.finished) setPill("done", `Done · ${sm.leads || 0} leads`);
      else setPill("idle", "Idle");
      if (st.error) { $("#formError").textContent = st.error; $("#formError").classList.remove("hidden"); }
    } else {
      setPill("running", "Scraping…");
    }
  }

  function startPolling() { if (!poll) poll = setInterval(tick, 1000); }
  function stopPolling() { clearInterval(poll); poll = null; }

  /* ---------------- actions ---------------- */
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#formError").classList.add("hidden");
    $("#apiBanner").classList.add("hidden");
    if (applyLock()) {
      showFormError(
        "This account has no active subscription. Contact your administrator to enable scraping.");
      return;
    }
    logOut.textContent = "";
    logIndex = 0;
    leads = [];
    render();

    let res;
    try {
      res = await api(`${API_BASE}/api/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(formData()),
      });
    } catch (e) {
      showFormError(
        e.offline
          ? "Lost contact with Nestick. Keep the console window open, then reload this page."
          : e.message);
      return;
    }
    if (!res.ok) { showFormError(res.message || res.error || "Could not start."); return; }
    setRunning(true);
    setPill("running", "Scraping…");
    startPolling();
  });

  stopBtn.addEventListener("click", async () => {
    stopBtn.disabled = true;
    stopBtn.textContent = "Stopping…";
    await fetch(`${API_BASE}/api/stop`, { method: "POST", headers: authHeaders() }).catch(() => {});
    setTimeout(() => { stopBtn.disabled = false; stopBtn.textContent = "Stop"; }, 1500);
  });

  document.querySelectorAll(".dl").forEach((a) => {
    a.classList.add("off");
    a.addEventListener("click", async () => {
      if (a.classList.contains("off")) return;
      try {
        const res = await fetch(`${API_BASE}/api/download/${a.dataset.fmt}`,
          { headers: authHeaders() });
        if (res.status === 401) { showLogin(); return; }
        if (!res.ok) return;
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const tmp = document.createElement("a");
        tmp.href = url;
        tmp.download = `nestick-${Date.now()}.${a.dataset.fmt}`;
        document.body.appendChild(tmp);
        tmp.click();
        tmp.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
      } catch {}
    });
  });

  /* ---------------- API keys ---------------- */
  const modal = $("#keysModal");
  $("#keysBtn").addEventListener("click", async () => {
    try {
      const have = await (await fetch(`${API_BASE}/api/settings`, { headers: authHeaders() })).json();
      for (const [id, key] of [["#k-serpapi", "serpapi_key"], ["#k-hunter", "hunter_key"],
                               ["#k-maps", "google_maps_key"],
                               ["#k-numverify", "numverify_key"]]) {
        $(id).placeholder = have[key] ? "•••••••• saved" : "not set";
        $(id).value = "";
      }
    } catch {}
    modal.classList.remove("hidden");
  });
  $("#keysCancel").addEventListener("click", () => modal.classList.add("hidden"));
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });
  $("#keysSave").addEventListener("click", async () => {
    const body = {};
    const v = (id) => $(id).value.trim();
    if (v("#k-serpapi")) body.serpapi_key = v("#k-serpapi");
    if (v("#k-hunter")) body.hunter_key = v("#k-hunter");
    if (v("#k-maps")) body.google_maps_key = v("#k-maps");
    if (v("#k-numverify")) body.numverify_key = v("#k-numverify");
    await fetch(`${API_BASE}/api/settings`, {
      method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    }).catch(() => {});
    modal.classList.add("hidden");
  });




  function showFormError(msg) {
    const el = $("#formError");
    if (!el) return;
    el.textContent = msg;
    el.classList.remove("hidden");
  }

  function setOffline(on) {
    const banner = $("#apiBanner");
    if (!banner) return;
    if (on) {
      banner.innerHTML =
        "<strong>Reconnecting\u2026</strong>" +
        "<p class=\"tip\">No reply from Nestick for a few seconds. This is usually " +
        "temporary during a heavy crawl \u2014 it will clear on its own. If it " +
        "persists, check the console window is still open.</p>";
      banner.classList.remove("hidden");
      setPill("error", "Offline");
    } else if (banner.dataset.kind === "offline") {
      banner.classList.add("hidden");
    }
    banner.dataset.kind = on ? "offline" : "";
  }

  /* ---------------- resilient API access ---------------- */
  async function api(path, options = {}, retries = 2) {
    let lastErr = "";
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const { headers, ...rest } = options;
        const res = await fetch(path, {
          cache: "no-store",
          headers: { ...authHeaders(), ...headers },
          ...rest,
        });
        const text = await res.text();
        let data = null;
        try { data = text ? JSON.parse(text) : null; } catch {
          // The server always answers JSON; anything else is a proxy or
          // extension interfering with local traffic.
          throw new Error(`Unexpected reply from the server (HTTP ${res.status}).`);
        }
        if (res.status === 401) {
          token = "";
          try { localStorage.removeItem("nestick.token"); } catch {}
          showLogin();
          throw new Error(data?.error || "Authentication required. Please sign in.");
        }
        if (!res.ok) throw new Error(data?.error || data?.message || `HTTP ${res.status}`);
        return data;
      } catch (e) {
        lastErr = e && e.message ? e.message : String(e);
        // Back off briefly; the server may still be starting up.
        if (attempt < retries) await new Promise((r) => setTimeout(r, 350 * (attempt + 1)));
      }
    }
    const err = new Error(lastErr || "The local server is not responding.");
    err.offline = true;
    throw err;
  }

  /* ---------------- authentication ---------------- */
  function setVerif(text) {
    const st = $("#verifStatus");
    if (st) st.textContent = text;
  }

  function showLogin() {
    setLoginMode("signin");
    const s = $("#loginScreen");
    if (s) {
      s.classList.remove("hidden", "granted", "denied");
    }
    const err = $("#loginError");
    if (err) err.classList.add("hidden");
    setVerif("CHANNEL SECURED \u00b7 AWAITING CREDENTIALS");
    const p = $("#loginPassword");
    if (p) setTimeout(() => p.focus(), 0);
  }

  function hideLogin() { $("#loginScreen")?.classList.add("hidden"); }

  function setGate(klass, status) {
    const s = $("#loginScreen");
    if (!s) return;
    s.classList.remove("granted", "denied");
    void s.offsetWidth;            // restart the shake animation
    if (klass) s.classList.add(klass);
    setVerif(status);
  }

  function updateAuthUI() {
    const btn = $("#logoutBtn");
    if (btn) btn.classList.toggle("hidden", !token);
  }

  function setLoginMode(mode) {
    loginMode = mode;
    document.querySelectorAll(".gm").forEach((b) => {
      const on = b.dataset.gm === mode;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    const nameField = $("#fieldName");
    if (nameField) nameField.classList.toggle("hidden", mode !== "register");
    const t = $("#loginBtnText");
    if (t) t.textContent = mode === "register" ? "CREATE ACCOUNT" : "UNLOCK TERMINAL";
  }

  function applyPlan() {
    const pill = $("#planPill");
    const banner = $("#subBanner");
    if (!me) {
      if (pill) pill.classList.add("hidden");
      if (banner) banner.classList.add("hidden");
      runBtn.title = "";
      setRunning(running);
      return;
    }
    const locked = applyLock();
    if (pill) {
      pill.textContent = me.enabled ? (me.plan || "free").toUpperCase() : "LOCAL";
      pill.className = "pill plan " + (locked ? "locked" : "ok");
      pill.classList.remove("hidden");
    }
    if (banner) {
      if (locked) {
        banner.innerHTML = "<strong>Subscription required</strong>" +
          "<p class=\"tip\">Your account is not subscribed. Scraping is disabled until an " +
          "administrator grants your account a plan in the database. Any results already " +
          "produced remain downloadable.</p>";
        banner.classList.remove("hidden");
      } else {
        banner.classList.add("hidden");
      }
    }
    runBtn.title = locked ? "Scraping requires an active subscription" : "";
    setRunning(running);
  }

  async function refreshMe() {
    try {
      const data = await api(`${API_BASE}/api/me`, {}, 1);
      me = data && typeof data === "object" ? data : null;
    } catch {
      me = null;
    }
    applyPlan();
  }

  const loginForm = $("#loginForm");
  if (loginForm) loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#loginError");
    const btn = $("#loginBtn");
    const text = $("#loginBtnText");
    const isRegister = loginMode === "register";
    if (err) err.classList.add("hidden");
    if (btn) btn.disabled = true;
    if (text) text.textContent = isRegister ? "CREATING\u2026" : "AUTHORIZING\u2026";
    setVerif(isRegister ? "REGISTERING CLEARANCE\u2026" : "SCANNING CREDENTIALS\u2026");
    try {
      const body = {
        email: $("#loginEmail").value,
        password: $("#loginPassword").value,
      };
      if (isRegister) body.name = $("#loginName").value;
      const res = await fetch(`${API_BASE}${isRegister ? "/api/register" : "/api/login"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.token) {
        setGate("denied", "ACCESS DENIED \u00b7 IDENTITY NOT RECOGNISED");
        if (err) { err.textContent = data.error || (isRegister ? "Could not create the account." : "Sign in failed."); err.classList.remove("hidden"); }
        return;
      }
      token = data.token;
      try { localStorage.setItem("nestick.token", token); } catch {}
      me = data.user && typeof data.user === "object" ? data.user : null;
      applyPlan();
      setGate("granted", isRegister
        ? "ACCESS GRANTED \u00b7 ACCOUNT CREATED"
        : "ACCESS GRANTED \u00b7 IDENTITY VERIFIED");
      if (text) text.textContent = "ACCESS GRANTED";
      await new Promise((r) => setTimeout(r, 750));
      hideLogin();
      updateAuthUI();
    } catch (e2) {
      setGate("denied", "ACCESS DENIED \u00b7 LINK SECURED");
      if (err) { err.textContent = e2 && e2.message ? e2.message : "Could not reach the server."; err.classList.remove("hidden"); }
    } finally {
      if (btn) btn.disabled = false;
      if (text) text.textContent = isRegister ? "CREATE ACCOUNT" : "UNLOCK TERMINAL";
    }
  });

  document.querySelectorAll(".gm").forEach((b) =>
    b.addEventListener("click", () => setLoginMode(b.dataset.gm)));

  const logoutBtn = $("#logoutBtn");
  if (logoutBtn) logoutBtn.addEventListener("click", () => {
    token = "";
    me = null;
    try { localStorage.removeItem("nestick.token"); } catch {}
    updateAuthUI();
    applyPlan();
    showLogin();
  });
  updateAuthUI();

  /* ---------------- intelligence report ---------------- */
  function pct(n, d) { return d ? Math.round((n / d) * 100) : 0; }

  function renderAnalytics(a) {
    const box = $("#analyticsOut");
    if (!box) return;
    if (!a || !a.total) { box.innerHTML = '<p class="muted">Run a scrape to see the report.</p>'; return; }
    const bands = a.score_bands || {};
    const chips = (o) => Object.entries(o || {}).map(([k, v]) =>
      `<span class="an-chip">${esc(k)} &middot; ${v}</span>`).join("") ||
      '<span class="muted">none</span>';
    const en = a.enrichment || {};
    box.innerHTML = `
      <div class="an-grid">
        <div class="an-card">
          <h4>Reachability</h4>
          <div class="an-row"><span>Contactable</span><b>${a.contactable}/${a.total}</b></div>
          <div class="an-bar"><span style="width:${a.contactable_pct}%"></span></div>
          <div class="an-row"><span>With e-mail</span><b>${a.with_email}</b></div>
          <div class="an-row"><span>With phone</span><b>${a.with_phone}</b></div>
          <div class="an-row"><span>With social</span><b>${a.with_social}</b></div>
        </div>
        <div class="an-card">
          <h4>Deliverability</h4>
          <div class="an-row"><span>MX verified</span><b>${a.deliverable_domains}/${a.total}</b></div>
          <div class="an-bar"><span style="width:${a.deliverable_pct}%"></span></div>
          <div class="an-row"><span>Checked</span><b>${en.mx_checked ?? "&mdash;"}</b></div>
          <div class="an-row"><span>No mail server</span><b>${en.undeliverable_domains ?? 0}</b></div>
        </div>
        <div class="an-card">
          <h4>E-mail quality</h4>
          <div class="an-row"><span>Unique</span><b>${a.unique_emails}</b></div>
          <div class="an-row"><span>Role (info@, sales@)</span><b>${a.role_emails}</b></div>
          <div class="an-row"><span>Named people</span><b>${a.personal_emails}</b></div>
          <div class="an-row"><span>Free mailboxes</span><b>${a.freemail_emails}</b></div>
        </div>
        <div class="an-card">
          <h4>Lead scoring</h4>
          <div class="an-row"><span>Average</span><b>${a.avg_score}</b></div>
          <div class="an-row"><span>Median</span><b>${a.median_score}</b></div>
          <div class="an-row"><span>Hot 60+</span><b>${bands["hot (60+)"] ?? 0}</b></div>
          <div class="an-row"><span>Warm 30-59</span><b>${bands["warm (30-59)"] ?? 0}</b></div>
          <div class="an-row"><span>Cold &lt;30</span><b>${bands["cold (<30)"] ?? 0}</b></div>
        </div>
        <div class="an-card">
          <h4>Mail platforms</h4>${chips(a.mail_platforms)}
        </div>
        <div class="an-card">
          <h4>Social presence</h4>${chips(a.social_networks)}
        </div>
        <div class="an-card">
          <h4>Domains</h4>${chips(a.top_tlds)}
        </div>
        <div class="an-card">
          <h4>Sources</h4>${chips(a.sources)}
          <div class="an-row" style="margin-top:6px"><span>Pages crawled</span><b>${a.pages_crawled}</b></div>
        </div>
      </div>`;
  }

  /* ---------------- tutorial tooltips ---------------- */
  const tip = $("#tip");

  function showTip(btn) {
    const text = btn.getAttribute("data-tip");
    if (!text || !tip) return;
    tip.textContent = text;
    tip.classList.add("show");
    const r = btn.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    // Prefer to the right; flip left when it would run off screen.
    let left = r.right + 10;
    if (left + tr.width > window.innerWidth - 12) left = r.left - tr.width - 10;
    if (left < 8) left = 8;
    let top = r.top + r.height / 2 - tr.height / 2;
    top = Math.max(8, Math.min(top, window.innerHeight - tr.height - 8));
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }
  function hideTip() { if (tip) tip.classList.remove("show"); }

  document.addEventListener("mouseover", (e) => {
    const btn = e.target.closest(".info");
    if (btn) showTip(btn);
  });
  document.addEventListener("mouseout", (e) => {
    if (e.target.closest(".info")) hideTip();
  });
  document.addEventListener("focusin", (e) => {
    const btn = e.target.closest(".info");
    if (btn) showTip(btn);
  });
  document.addEventListener("focusout", hideTip);
  // Tap-to-toggle on touch devices, and never submit the form.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".info");
    if (!btn) { hideTip(); return; }
    e.preventDefault();
    e.stopPropagation();
    tip.classList.contains("show") ? hideTip() : showTip(btn);
  });
  window.addEventListener("scroll", hideTip, true);
  window.addEventListener("resize", hideTip);

  /* ---------------- first-run guided tour ---------------- */
  const TOUR = [
    { sel: ".tabs", title: "1. Choose how to start",
      body: "Search finds businesses from a query like \u201cdentists in Lahore\u201d. " +
            "URL list scrapes websites you already have." },
    { sel: "[name=query]", title: "2. Describe what you want",
      body: "Type it in plain words and include the city. One query per line runs several searches." },
    { sel: ".advanced", title: "3. Tune it only if you need to",
      body: "Every setting has a sensible default. Hover any \u201ci\u201d button to learn what it does." },
    { sel: "#runBtn", title: "4. Run it",
      body: "Results stream in live, best leads first. You can press Stop at any time and keep what was found." },
    { sel: ".downloads", title: "5. Take your leads",
      body: "Export to CSV, Excel or JSON in one click. Excel comes formatted with clickable links." },
  ];
  let tourStep = 0, tourEls = null;

  function endTour(remember) {
    if (tourEls) { tourEls.forEach((el) => el.remove()); tourEls = null; }
    if (remember) { try { localStorage.setItem("nestick.tour", "done"); } catch {} }
  }

  function renderTour() {
    endTour(false);
    if (tourStep >= TOUR.length) { endTour(true); return; }
    const step = TOUR[tourStep];
    const target = document.querySelector(step.sel);
    const mask = document.createElement("div");
    mask.className = "tour-mask";
    const spot = document.createElement("div");
    spot.className = "tour-spot";
    const card = document.createElement("div");
    card.className = "tour-card";
    card.innerHTML =
      `<h3>${esc(step.title)}</h3><p>${esc(step.body)}</p>
       <div class="tour-row">
         <div class="tour-dots">${TOUR.map((_, i) =>
           `<span class="tour-dot ${i === tourStep ? "on" : ""}"></span>`).join("")}</div>
         <div>
           <button type="button" class="ghost" id="tourSkip">Skip</button>
           <button type="button" class="primary" id="tourNext"
             style="width:auto;padding:7px 16px;display:inline-block">
             ${tourStep === TOUR.length - 1 ? "Finish" : "Next"}</button>
         </div>
       </div>`;
    document.body.append(mask, spot, card);
    tourEls = [mask, spot, card];

    const visible = target && target.getBoundingClientRect().height > 0;
    if (visible) {
      target.scrollIntoView({ block: "center", behavior: "instant" });
      const r = target.getBoundingClientRect();
      const pad = 6;
      spot.style.left = (r.left - pad) + "px";
      spot.style.top = (r.top - pad) + "px";
      spot.style.width = (r.width + pad * 2) + "px";
      spot.style.height = (r.height + pad * 2) + "px";
      // Place the card beside the highlight, then clamp it fully on-screen so
      // it can never render below the fold on a short window.
      const cw = 350, ch = card.offsetHeight || 190;
      let cl = r.right + 18;
      if (cl + cw > window.innerWidth - 12) cl = r.left - cw - 18;
      if (cl < 12) cl = Math.max(12, (window.innerWidth - cw) / 2);
      let ct = r.top;
      if (ct + ch > window.innerHeight - 12) ct = window.innerHeight - ch - 12;
      card.style.left = Math.round(cl) + "px";
      card.style.top = Math.round(Math.max(12, ct)) + "px";
    } else {
      spot.style.display = "none";
      card.style.left = "50%";
      card.style.top = "50%";
      card.style.transform = "translate(-50%,-50%)";
    }
    card.querySelector("#tourNext").onclick = () => { tourStep++; renderTour(); };
    card.querySelector("#tourSkip").onclick = () => endTour(true);
    mask.onclick = () => endTour(true);
  }

  function startTour() { tourStep = 0; renderTour(); }
  const helpBtn = $("#helpBtn");
  if (helpBtn) helpBtn.addEventListener("click", startTour);
  try {
    if (!localStorage.getItem("nestick.tour")) setTimeout(startTour, 700);
  } catch {}

  /* resume view if a job is already running (e.g. page reload) */
  if (token) refreshMe();
  fetch(`${API_BASE}/api/status`, { headers: authHeaders() }).then((r) => {
    if (r.status === 401) {
      token = "";
      try { localStorage.removeItem("nestick.token"); } catch {}
      showLogin();
      updateAuthUI();
      return null;
    }
    return r.json();
  }).then((st) => {
    if (!st) return;
    if (st.running) { setRunning(true); startPolling(); }
    else if (st.leads && st.leads.length) { leads = st.leads; render(); }
  }).catch(() => {});
})();
