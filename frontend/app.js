(() => {
  "use strict";

  // Fixed label order -- must match src/labels.py PRIMARY_LABELS and the
  // --series-* custom properties in style.css. Order is the CVD-safety
  // mechanism for the categorical palette (anthropic-skills:dataviz), so
  // it's never re-sorted by count.
  const LABEL_ORDER = [
    "fit_issue",
    "quality_defect",
    "shipping_delivery",
    "customer_service",
    "product_as_described_mismatch",
    "general_sentiment_other",
  ];
  const LABEL_TITLES = {
    fit_issue: "Fit issue",
    quality_defect: "Quality defect",
    shipping_delivery: "Shipping / delivery",
    customer_service: "Customer service",
    product_as_described_mismatch: "Described mismatch",
    general_sentiment_other: "General sentiment",
  };

  const MAX_CARDS = 40;

  // The only two run sizes this page offers. Demo mode picks between
  // them -- there is no separate "mode" concept in the UI: the button
  // always triggers a real, live Jev classification, just capped to a
  // small count when demo mode is on.
  const DEMO_RUN_N = 20;
  const FULL_RUN_N = 12000;

  const params = new URLSearchParams(window.location.search);
  const revealDelayMs = params.get("reveal_delay_ms") || "120";
  const maxBatchTokens = params.get("max_batch_tokens") || "28000";

  const el = {
    statusWrap: document.getElementById("status"),
    statusDot: document.getElementById("statusDot"),
    statusLabel: document.getElementById("statusLabel"),
    statCount: document.getElementById("statCount"),
    statTotal: document.getElementById("statTotal"),
    statRate: document.getElementById("statRate"),
    statElapsed: document.getElementById("statElapsed"),
    statCost: document.getElementById("statCost"),
    progressFill: document.getElementById("progressFill"),
    feed: document.getElementById("feed"),
    bars: document.getElementById("bars"),
    startBar: document.getElementById("startBar"),
    startTitle: document.getElementById("startTitle"),
    startDesc: document.getElementById("startDesc"),
    startBtn: document.getElementById("startBtn"),
    demoToggleInput: document.getElementById("demoToggleInput"),
    resultsBody: document.getElementById("resultsBody"),
    tableCount: document.getElementById("tableCount"),
  };

  let total = 0;
  let estCostPerItem = 0;
  let labelCounts = {};
  let startTime = null;
  let elapsedTimer = null;

  // Holds every classified review for the full results table (the animated
  // feed above only keeps the most recent MAX_CARDS). Sorted in place and
  // fully re-rendered only when a header is clicked -- appending as results
  // arrive is O(1) per row, so the live stream stays cheap regardless.
  const allResults = [];
  let sortKey = null;
  let sortDir = 1;

  function setStatus(kind, text) {
    el.statusWrap.className = kind;
    el.statusLabel.textContent = text;
  }

  function bump(node) {
    node.classList.remove("stat-bump");
    // force reflow so the animation can restart on rapid updates
    void node.offsetWidth;
    node.classList.add("stat-bump");
  }

  function formatElapsed(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function initBars() {
    el.bars.innerHTML = "";
    LABEL_ORDER.forEach((label) => {
      labelCounts[label] = 0;
      const row = document.createElement("div");
      row.className = "bar-row";
      row.innerHTML = `
        <div class="bar-row-head">
          <div class="bar-row-name">
            <span class="bar-row-dot" style="--bar-color:var(--series-${label})"></span>
            ${LABEL_TITLES[label] || label}
          </div>
          <span class="bar-row-count" id="barCount-${label}">0</span>
        </div>
        <div class="bar-track">
          <div class="bar-fill" id="barFill-${label}" style="--bar-color:var(--series-${label})"></div>
        </div>`;
      el.bars.appendChild(row);
    });
  }

  function updateBars() {
    const max = Math.max(1, ...Object.values(labelCounts));
    LABEL_ORDER.forEach((label) => {
      const count = labelCounts[label] || 0;
      const fill = document.getElementById(`barFill-${label}`);
      const countEl = document.getElementById(`barCount-${label}`);
      if (fill) fill.style.width = `${(count / max) * 100}%`;
      if (countEl) countEl.textContent = count.toLocaleString();
    });
  }

  function starString(rating) {
    const n = Math.round(rating || 0);
    return "★".repeat(n) + "☆".repeat(Math.max(0, 5 - n));
  }

  function detailTag(review) {
    if (review.primary_choice === "fit_issue" && review.fit_direction_score != null) {
      const v = review.fit_direction_score;
      const dir = v < 0.75 ? "Runs small" : v > 1.25 ? "Runs large" : "True to size";
      return `<span class="detail-tag">${dir}</span>`;
    }
    if (review.primary_choice === "quality_defect" && review.severity_score != null) {
      const v = review.severity_score;
      const sev = v < 1 ? "Minor" : v < 2 ? "Moderate" : "Severe";
      return `<span class="detail-tag">${sev} severity</span>`;
    }
    return "";
  }

  function confidencePct(review) {
    return review.primary_confidence != null ? `${Math.round(review.primary_confidence * 100)}%` : "—";
  }

  function addCard(review) {
    if (el.feed.querySelector(".feed-empty")) el.feed.innerHTML = "";

    const card = document.createElement("div");
    card.className = "card";

    card.innerHTML = `
      <div class="card-top">
        <div class="card-meta">
          <span class="stars">${starString(review.rating)}</span>
        </div>
      </div>
      <p class="card-text">${escapeHtml(review.text || "")}</p>
      <div class="card-bottom">
        <span class="pill" style="--pill-color:var(--series-${review.primary_choice})">
          <span class="pill-dot"></span>${LABEL_TITLES[review.primary_choice] || review.primary_choice}
        </span>
        <span class="confidence">${confidencePct(review)} confidence</span>
        ${detailTag(review)}
      </div>`;

    el.feed.insertBefore(card, el.feed.firstChild);
    while (el.feed.children.length > MAX_CARDS) {
      el.feed.removeChild(el.feed.lastChild);
    }
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function rowHtml(review, idx) {
    return `
      <td class="col-idx">${idx + 1}</td>
      <td><span class="stars">${starString(review.rating)}</span></td>
      <td><span class="cell-text" title="${escapeHtml(review.text || "")}">${escapeHtml(review.text || "")}</span></td>
      <td>
        <span class="pill" style="--pill-color:var(--series-${review.primary_choice})">
          <span class="pill-dot"></span>${LABEL_TITLES[review.primary_choice] || review.primary_choice}
        </span>
      </td>
      <td><span class="confidence">${confidencePct(review)}</span></td>
      <td>${detailTag(review) || "&mdash;"}</td>`;
  }

  function addResultRow(review) {
    if (el.resultsBody.querySelector(".table-empty-row")) el.resultsBody.innerHTML = "";
    allResults.push(review);

    // Only append (cheap) when the table is showing arrival order -- if the
    // viewer has sorted by a column, a full re-render keeps new rows in the
    // right place instead of always tacking them onto the end.
    if (sortKey === null) {
      const tr = document.createElement("tr");
      tr.innerHTML = rowHtml(review, allResults.length - 1);
      el.resultsBody.appendChild(tr);
    } else {
      renderTable();
    }
    el.tableCount.textContent = `${allResults.length.toLocaleString()} row${allResults.length === 1 ? "" : "s"}`;
  }

  function renderTable() {
    const rows = allResults.slice();
    if (sortKey) {
      rows.sort((a, b) => {
        const av = a[sortKey];
        const bv = b[sortKey];
        if (av == null && bv == null) return 0;
        if (av == null) return 1;
        if (bv == null) return -1;
        if (typeof av === "string") return av.localeCompare(bv) * sortDir;
        return (av - bv) * sortDir;
      });
    }
    el.resultsBody.innerHTML = rows.map((r, i) => `<tr>${rowHtml(r, i)}</tr>`).join("");
  }

  function setUpTableSorting() {
    document.querySelectorAll(".results-table th.sortable").forEach((th) => {
      th.innerHTML = `${th.textContent}<span class="sort-arrow">↕</span>`;
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (sortKey === key) {
          sortDir *= -1;
        } else {
          sortKey = key;
          sortDir = 1;
        }
        document.querySelectorAll(".results-table th.sortable").forEach((other) => {
          other.classList.toggle("sort-active", other === th);
          other.querySelector(".sort-arrow").textContent = other !== th ? "↕" : sortDir === 1 ? "↑" : "↓";
        });
        renderTable();
      });
    });
  }

  function startElapsedClock() {
    startTime = Date.now();
    elapsedTimer = setInterval(() => {
      el.statElapsed.textContent = formatElapsed((Date.now() - startTime) / 1000);
    }, 1000);
  }

  function connect(effectiveN, demoMode) {
    // The demo flag is sent alongside n, not just the pre-capped number --
    // the server enforces the same cap independently, so this isn't the
    // only thing standing between a stray request and a full-size run.
    // mode is always "live" here: this button never does anything but a
    // real classification run (replay still exists server-side as a
    // manual fallback -- see server/app.py -- it's just not offered here).
    const url = `/api/stream?mode=live&n=${encodeURIComponent(effectiveN)}`
      + `&demo=${demoMode ? "1" : "0"}`
      + `&reveal_delay_ms=${encodeURIComponent(revealDelayMs)}&max_batch_tokens=${encodeURIComponent(maxBatchTokens)}`;
    const source = new EventSource(url);

    setStatus("connecting", "Connecting…");

    source.addEventListener("meta", (e) => {
      const data = JSON.parse(e.data);
      total = data.total;
      estCostPerItem = data.est_cost_per_item || 0;
      el.statTotal.textContent = total.toLocaleString();
      initBars();
      setStatus("live", "Live");
      startElapsedClock();
    });

    source.addEventListener("review", (e) => {
      const review = JSON.parse(e.data);
      addCard(review);
      addResultRow(review);
      labelCounts[review.primary_choice] = (labelCounts[review.primary_choice] || 0) + 1;
      updateBars();

      el.statCount.textContent = review.count.toLocaleString();
      bump(el.statCount.parentElement);
      el.statRate.textContent = review.rate_per_min.toFixed(1);
      el.progressFill.style.width = `${(review.count / review.total) * 100}%`;
      el.statCost.textContent = `$${(review.count * estCostPerItem).toFixed(4)}`;
    });

    source.addEventListener("error", (e) => {
      let message = "Connection error";
      try { message = JSON.parse(e.data).error; } catch (_) { /* SSE transport error, no payload */ }
      setStatus("error", message);
    });

    source.addEventListener("done", (e) => {
      const data = JSON.parse(e.data);
      clearInterval(elapsedTimer);
      setStatus("done", `Done — ${data.total_classified.toLocaleString()} classified in ${formatElapsed(data.elapsed_s)}`);
      source.close();
    });

    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        setStatus("error", "Connection lost");
      }
    };
  }

  function isDemoMode() {
    return el.demoToggleInput.checked;
  }

  function effectiveN() {
    return isDemoMode() ? DEMO_RUN_N : FULL_RUN_N;
  }

  function loadDemoModePref() {
    try {
      const saved = localStorage.getItem("rcc_demo_mode");
      // Default ON: the safer starting state given this run can spend real money.
      return saved === null ? true : saved === "1";
    } catch (_) {
      return true;
    }
  }

  function saveDemoModePref(value) {
    try { localStorage.setItem("rcc_demo_mode", value ? "1" : "0"); } catch (_) { /* ignore */ }
  }

  function renderStartGate() {
    const n = effectiveN();
    el.startTitle.textContent = "Start classification";
    el.startDesc.innerHTML = `Calls the real Jev API for <strong>${n.toLocaleString()}</strong> reviews `
      + `and <span class="cost-warn">spends real API credit</span> the moment you click. `
      + `Nothing happens before that.`;
    el.startBtn.textContent = `Start — ${n.toLocaleString()} reviews`;
    el.startBtn.className = "start-btn ready-live";
  }

  function setUpStartGate() {
    el.demoToggleInput.checked = loadDemoModePref();
    renderStartGate();

    el.demoToggleInput.addEventListener("change", () => {
      saveDemoModePref(el.demoToggleInput.checked);
      renderStartGate();
    });

    el.startBtn.disabled = false;
    el.startBtn.addEventListener("click", () => {
      const n = effectiveN();
      const demoMode = isDemoMode();
      el.startBtn.disabled = true;
      el.demoToggleInput.disabled = true;
      el.startBtn.textContent = "Starting…";
      el.startBar.classList.add("hidden");
      el.feed.innerHTML = '<div class="feed-empty">Waiting for the first classification&hellip;</div>';
      connect(n, demoMode);
    }, { once: true });
  }

  // Nothing above this line opens a connection or calls any API --
  // connect() only runs from the button's click handler above.
  setUpStartGate();
  setUpTableSorting();
})();
