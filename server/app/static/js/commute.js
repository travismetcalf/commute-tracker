/* Dashboard wiring. One rule above all: the MapLibre Map and the Chart
   instances are created ONCE and mutated on state change — never destroyed
   and rebuilt (a swapped-out Map leaks its WebGL context). State lives in
   the URL (?dir=home|office&route=slug); localStorage only mirrors it.

   Direction is ONE piece of state with TWO switcher instances (the NOW and
   PATTERNS zone headers). Every read/write goes through setState/renderControls,
   so the instances can never disagree. */

(function () {
  "use strict";

  const boot = JSON.parse(document.getElementById("boot-data").textContent);
  const DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri"];
  const HOURS = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18];
  const hourLabel = h => (h < 12 ? h + " AM" : h === 12 ? "12 PM" : (h - 12) + " PM");
  const HOUR_LABELS = HOURS.map(hourLabel);
  // "When to leave" is two half-days, not one flat 7-to-6 line. These domains
  // must match what the server can EMIT — stats.hourly() buckets range(7,19)
  // and WEEKDAY_HOURS filters local_hour BETWEEN 7 AND 18 in SQL. Plotting 6 AM
  // or 7 PM would leave a permanently empty column at one edge of each panel,
  // offsetting the two lines from each other and defeating the shared y-scale.
  // Widening these means changing stats.py's bucket list AND the SQL predicate.
  const AM_HOURS = [7, 8, 9, 10];
  const PM_HOURS = [15, 16, 17, 18];

  /* ---- state ---------------------------------------------------------- */
  const params = new URLSearchParams(location.search);
  let dir = params.get("dir") || localStorage.getItem("ct.dir")
         || (new Date().getHours() < 12 ? "office" : "home");  // clock default
  let route = params.get("route") || localStorage.getItem("ct.route." + dir) || null;

  function routesFor(d) { return d === "office" ? boot.routes.outbound : boot.routes.inbound; }

  // "The" corridor for a heading, by ROLE. Slugs are generated from whatever
  // the user named their places, so no literal one can appear on this side.
  function primaryRoute(heading) {
    const list = heading === "outbound" ? boot.routes.outbound : boot.routes.inbound;
    return list.find(r => r.role === "primary") || list[0] || null;
  }

  function currentRoute() {
    const list = routesFor(dir);
    return list.find(r => r.slug === route) || list[0] || null;
  }

  function setState(nextDir, nextRoute) {
    dir = nextDir;
    const list = routesFor(dir);
    route = (nextRoute && list.some(r => r.slug === nextRoute)) ? nextRoute
          : (localStorage.getItem("ct.route." + dir) || (list[0] && list[0].slug));
    localStorage.setItem("ct.dir", dir);
    if (route) localStorage.setItem("ct.route." + dir, route);
    const q = new URLSearchParams({ dir });
    if (route) q.set("route", route);
    history.replaceState({}, "", "?" + q.toString());
    renderControls();
    refreshAll();
  }

  function renderControls() {
    // BOTH switcher instances, always — one state, two mirrors.
    document.querySelectorAll(".dir-seg .seg__btn").forEach(b =>
      b.setAttribute("aria-pressed", String(b.dataset.dir === dir)));
    const sel = document.getElementById("route-select");
    const list = routesFor(dir);
    sel.innerHTML = "";
    for (const r of list) {
      const o = document.createElement("option");
      o.value = r.slug; o.textContent = r.label;
      sel.appendChild(o);
    }
    if (route) sel.value = route;
    sel.hidden = list.length < 2;
    // every direction-scoped card echoes the state so a mid-page glance never
    // needs the zone header
    const r = currentRoute();
    const label = r ? r.label : "—";
    document.querySelectorAll("[data-context]").forEach(el =>
      el.textContent = label + " · weekdays 7 AM–7 PM");
  }

  document.querySelectorAll(".dir-seg .seg__btn").forEach(b =>
    b.addEventListener("click", () => setState(b.dataset.dir, null)));
  document.getElementById("route-select").addEventListener("change", e =>
    setState(dir, e.target.value));

  /* ---- formatting ------------------------------------------------------ */
  const min = s => s == null ? null : Math.round(s / 60);
  const fmtMin = s => { const m = min(s); return m == null ? "—" :
    m > 99 ? Math.floor(m / 60) + " h " + (m % 60) + " m" : m + " min"; };

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  let C = {};
  function loadColors() {
    C = {
      predicted: cssVar("--series-predicted"),
      actual: cssVar("--series-actual"),
      band: cssVar("--band-fill"),
      grid: cssVar("--rule"),
      tick: cssVar("--ink-faint"),
      heat: [1, 2, 3, 4, 5].map(i => cssVar("--heat-" + i)),
      heatEmpty: cssVar("--heat-empty"),
      ink: cssVar("--ink"),
      raised: cssVar("--paper-raised"),
      good: cssVar("--mark-good"),
      bad: cssVar("--mark-bad"),
    };
  }
  loadColors();

  /* ---- charts (created once) ------------------------------------------ */
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 11;

  function axisOpts() {
    return {
      x: { grid: { display: false }, ticks: { color: C.tick } },
      y: { grid: { color: C.grid, lineWidth: 1 }, border: { display: false },
           ticks: { color: C.tick, callback: v => v + " min" } },
    };
  }
  const tooltipStyle = () => ({
    backgroundColor: C.raised, borderColor: C.grid, borderWidth: 1,
    titleColor: C.ink, bodyColor: C.ink, cornerRadius: 8, displayColors: false,
  });

  // Same hard-won lesson as the map: an unguarded constructor at IIFE top level
  // aborts the whole module. `new Chart(null, …)` throws, and a canvas id can
  // drift between template and JS at any time.
  function safeChart(canvasId, config) {
    const el = document.getElementById(canvasId);
    if (!el) { console.error("missing canvas: " + canvasId); return null; }
    try { return new Chart(el, config); }
    catch (err) { console.error("chart failed: " + canvasId, err); return null; }
  }

  function makeHourlyChart(canvasId, hours) {
    return safeChart(canvasId, {
      type: "line",
      data: { labels: hours.map(hourLabel), datasets: [
        { label: "p10", data: [], borderWidth: 0, pointRadius: 0, fill: false },
        { label: "p10–p90", data: [], borderWidth: 0, pointRadius: 0, fill: "-1",
          backgroundColor: C.band },
        { label: "median", data: [], borderColor: C.predicted, borderWidth: 2,
          pointRadius: 0, borderJoinStyle: "round", fill: false },
        { label: "actual", data: [], showLine: false, pointRadius: 5,
          pointBackgroundColor: C.actual, pointBorderColor: C.raised, pointBorderWidth: 2 },
      ]},
      options: {
        responsive: true, maintainAspectRatio: false, spanGaps: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: false }, tooltip: { ...tooltipStyle(),
          filter: i => i.datasetIndex >= 2,
          callbacks: { label: ctx => {
            const meta = ctx.dataset._meta && ctx.dataset._meta[ctx.dataIndex];
            const base = ctx.dataset.label + ": " + fmtMin(ctx.parsed.y * 60);
            return meta ? base + " (n=" + meta + ")" : base;
          } } } },
        scales: (() => { const a = axisOpts(); a.y.beginAtZero = false; return a; })(),
      },
    });
  }

  const amChart = makeHourlyChart("hourly-am-chart", AM_HOURS);
  const pmChart = makeHourlyChart("hourly-pm-chart", PM_HOURS);
  // PLANNING is NOT direction-scoped: each panel is pinned to its own primary
  // route, so the pair always answers "morning out AND evening back".
  const HOURLY_PANELS = [
    { chart: amChart, hours: AM_HOURS, emptyId: "hourly-am-empty",
      routeLabelId: "panel-am-route", heading: "outbound" },
    { chart: pmChart, hours: PM_HOURS, emptyId: "hourly-pm-empty",
      routeLabelId: "panel-pm-route", heading: "inbound" },
  ];
  const panelRoute = p => primaryRoute(p.heading);

  let biasChart = null;
  if (boot.bias && boot.bias.length) {
    const pts = boot.bias.map(p => ({ x: min(p.predicted_sec), y: min(p.actual_sec) }));
    const maxV = Math.max(...pts.map(p => Math.max(p.x, p.y))) * 1.1;
    biasChart = safeChart("bias-chart", {
      type: "scatter",
      data: { datasets: [
        { label: "perfect prediction", type: "line", data: [{ x: 0, y: 0 }, { x: maxV, y: maxV }],
          borderColor: C.grid, borderWidth: 1, pointRadius: 0 },
        { label: "trip", data: pts, pointRadius: 5,
          pointBackgroundColor: pts.map(p => p.y > p.x ? C.bad : C.good),
          pointBorderColor: C.raised, pointBorderWidth: 2 },
      ]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { ...tooltipStyle(),
          callbacks: { label: ctx => ctx.datasetIndex === 1
            ? "predicted " + ctx.parsed.x + " min → actual " + ctx.parsed.y + " min" : null } } },
        scales: {
          x: { title: { display: true, text: "predicted (min)", color: C.tick },
               grid: { color: C.grid }, ticks: { color: C.tick } },
          y: { title: { display: true, text: "actual (min)", color: C.tick },
               grid: { color: C.grid }, ticks: { color: C.tick } },
        },
      },
    });
  }

  function restyleCharts() {
    loadColors();
    for (const p of HOURLY_PANELS) {
      if (!p.chart) continue;
      const ds = p.chart.data.datasets;
      ds[1].backgroundColor = C.band;
      ds[2].borderColor = C.predicted;
      ds[3].pointBackgroundColor = C.actual;
      ds[3].pointBorderColor = C.raised;
    }
    if (biasChart) {
      const pts = biasChart.data.datasets[1].data;
      biasChart.data.datasets[1].pointBackgroundColor = pts.map(p => p.y > p.x ? C.bad : C.good);
      biasChart.data.datasets[1].pointBorderColor = C.raised;
    }
    for (const ch of [amChart, pmChart, biasChart]) {
      if (!ch) continue;
      const so = axisOpts();
      if (ch.options.scales.x.grid) Object.assign(ch.options.scales.x, so.x);
      Object.assign(ch.options.scales.y, so.y);
      ch.update("none");
    }
    renderHeatmap();   // the heatmap is hand-painted, not a Chart.js canvas
  }
  // Only the casings follow the theme — corridor hues are theme-invariant
  // because the basemap they sit on is.
  window.addEventListener("ct-theme-change", () => {
    if (!map || !mapReady) return;
    for (const id of ["route-casing", "route-alt1-casing", "route-alt2-casing"])
      if (map.getLayer(id)) map.setPaintProperty(id, "line-color", casingColor());
    // The pills and the endpoint badges BAKE their casing ring into a bitmap, so
    // they have to be redrawn — a symbol layer has no paint property that could
    // carry it the way a line does.
    for (const kind of Object.keys(ENDPOINT_ICON)) addEndpointIcon(kind);
    renderRouteLabels(lastAlts);
  });
  window.addEventListener("ct-theme-change", restyleCharts);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", restyleCharts);

  /* ---- data refresh ---------------------------------------------------- */
  async function getJSON(url) {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(url + " → " + r.status);
    return r.json();
  }

  function fillHourlyPanel(p, d) {
    const byHour = Object.fromEntries(d.buckets.map(b => [b.hour, b]));
    const actualBy = Object.fromEntries((d.actual || []).map(b => [b.hour, b]));
    const val = (h, k) => {
      const b = byHour[h];
      return b && b.n >= 3 && b[k] != null ? min(b[k]) : null;  // n<3 → gap, not a fake point
    };
    if (!p.chart) { document.getElementById(p.emptyId).hidden = false; return; }
    const ds = p.chart.data.datasets;
    ds[0].data = p.hours.map(h => val(h, "p10_sec"));
    ds[1].data = p.hours.map(h => val(h, "p90_sec"));
    ds[2].data = p.hours.map(h => val(h, "p50_sec"));
    ds[2]._meta = p.hours.map(h => (byHour[h] || {}).n);
    ds[3].data = p.hours.map(h => {
      const a = actualBy[h];
      return a && a.n >= 3 ? min(a.p50_sec) : null;
    });
    const any = ds[2].data.some(v => v != null);
    document.getElementById(p.emptyId).hidden = any;
    p.chart.canvas.style.display = any ? "" : "none";
    syncHourlyScales();
  }

  // Two panels of the SAME measure sitting side by side must share a vertical
  // scale, or the eye compares shapes that aren't comparable.
  function syncHourlyScales() {
    const vals = [];
    for (const p of HOURLY_PANELS)
      if (p.chart) for (const ds of p.chart.data.datasets)
        for (const v of ds.data) if (v != null) vals.push(v);
    for (const p of HOURLY_PANELS) {
      if (!p.chart) continue;
      if (vals.length) {
        p.chart.options.scales.y.suggestedMin = Math.max(0, Math.floor(Math.min(...vals)) - 2);
        p.chart.options.scales.y.suggestedMax = Math.ceil(Math.max(...vals)) + 2;
      }
      p.chart.update();
    }
  }

  function emptyHourlyPanel(p) {
    if (p.chart) p.chart.canvas.style.display = "none";
    document.getElementById(p.emptyId).hidden = false;
  }

  function refreshHourly() {
    for (const p of HOURLY_PANELS) {
      const r = panelRoute(p);
      // These panels are pinned to the primary corridors and ignore the header
      // route selector — so they must SAY which corridor they show, or a reader
      // reasonably assumes they follow the selection like every other card.
      const tag = document.getElementById(p.routeLabelId);
      if (tag) tag.textContent = r ? "· " + r.label : "";
      if (!r) { emptyHourlyPanel(p); continue; }
      getJSON("/api/stats/hourly?route=" + encodeURIComponent(r.slug))
        .then(d => fillHourlyPanel(p, d)).catch(console.error);
    }
  }

  function refreshSpread() {
    const r = currentRoute();
    if (!r) return;
    getJSON("/api/stats/dow?route=" + encodeURIComponent(r.slug)).then(d => {
      lastDow = d.buckets;   // reserved: per-(weekday, departure-window) view
      renderHeatmap();
    }).catch(console.error);
  }

  function luminance(hex) {
    const h = hex.replace("#", "");
    const [r, g, b] = [0, 2, 4].map(i => {
      const c = parseInt(h.slice(i, i + 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }

  /* ---- heatmap: a real <table>, days as columns, hour rows -------------- */
  /* The weekday chart is not a separate view of the data — the heatmap's
     COLUMNS are the weekdays, so it belongs under the grid as the column
     Deliberately NO weekday marginal: grouping a whole day's samples into one
     per-weekday distribution measures TIME-OF-DAY variation, not day-to-day
     reliability — the exact averaging the ±15-min departure bucket exists to
     avoid. Compare days by reading ACROSS one hour row instead. */
  let lastHeatmap = null;
  let lastDow = null;


  function renderHeatmap(data) {
    if (data) lastHeatmap = data;
    const d = lastHeatmap;
    if (!d) return;
    const table = document.getElementById("heatmap");
    // aggregate 30-min slots to hourly cells
    const cells = {};
    for (const c of d.cells) {
      if (c.n === 0 || c.p50_sec == null) continue;
      const hour = Math.floor(c.slot30 / 2);
      const key = c.dow + ":" + hour;
      (cells[key] = cells[key] || []).push(c.p50_sec);
    }
    const vals = Object.values(cells).map(a => a.reduce((x, y) => x + y) / a.length);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const step = v => vals.length < 2 || hi === lo ? 2
      : Math.min(4, Math.floor((v - lo) / (hi - lo) * 5));

    let html = "<caption>Median commute by weekday and hour — green is faster, red is slower. " +
      "Values in minutes. Compare days by reading across one hour row — that is the " +
      "hour you actually leave.</caption>";
    html += "<thead><tr><th></th>" + DOW_LABELS.map(d => `<th scope="col">${d}</th>`).join("") + "</tr></thead><tbody>";
    for (const h of HOURS) {
      html += `<tr><th scope="row">${HOUR_LABELS[HOURS.indexOf(h)]}</th>`;
      for (let dd = 0; dd < 5; dd++) {
        const arr = cells[dd + ":" + h];
        if (!arr) { html += `<td tabindex="0" aria-label="no data"></td>`; continue; }
        const v = arr.reduce((x, y) => x + y) / arr.length;
        const s = step(v);
        const bg = C.heat[s];
        const fg = luminance(bg) > 0.45 ? "#1a1b2e" : "#fff";  // per-fill contrast pick
        html += `<td tabindex="0" style="background:${bg};color:${fg};border-color:transparent"` +
                ` aria-label="${DOW_LABELS[dd]} ${HOUR_LABELS[HOURS.indexOf(h)]}: ${min(v)} minutes">${min(v)}</td>`;
      }
      html += "</tr>";
    }
    table.innerHTML = html + "</tbody>";

    const legend = document.getElementById("heat-legend");
    legend.innerHTML = vals.length
      ? "faster " + C.heat.map(c => `<span class="heat__step" style="background:${c}"></span>`).join("") + " slower"
      : "";
  }

  function refreshHeatmap() {
    const r = currentRoute();
    if (!r) return;
    getJSON("/api/stats/heatmap?route=" + encodeURIComponent(r.slug))
      .then(renderHeatmap).catch(console.error);
  }

  /* ---- map (created once) ---------------------------------------------- */
  const EMPTY_FC = { type: "FeatureCollection", features: [] };
  // TWO color languages share this map, and they must not collide:
  //   traffic  — the flow raster underneath owns green→amber→RED. On a road map
  //              red means "jammed", so nothing here may wear it for any other
  //              reason. (It used to: the primary was crimson, which said
  //              "this whole corridor is a jam" to anyone reading map-first.)
  //   identity — which corridor a line IS. Confined to the blue→purple→pink arc
  //              the traffic ramp never touches.
  // Every hex clears 3:1 on the beige basemap, and all four pass the all-pairs
  // gate against it: worst ΔE 14.8 under protanopia, 16.7 under normal vision.
  // (ΔE is Euclidean OKLab x100, CVD simulated Machado-Oliveira-Fernandes 2009
  // at severity 1.0 — state the units, or the next reader recomputes in CIEDE2000
  // and concludes the comment is wrong.) The old palette was NOT a CVD failure —
  // crimson-vs-dark-crimson scored a healthy 12.5, because lightness survives
  // colorblindness. It failed on contrast (gold 1.75:1, invisible on beige) and
  // on meaning. alt1 is deliberately the DEEP purple: a lighter one reads better
  // in a 10px legend chip but drops primary-vs-alt1 to 9.9, and those two are the
  // pair a reader most needs to separate. The chip gets a ring instead.
  const CORRIDOR = {
    primary: "#2563eb",   // yours — the "selected route" blue every nav app uses
    slow:    "#c8271c",   // congestion, and the ONLY red we paint
    alt1:    "#8f1c8a",   // purple
    alt2:    "#e5559a",   // pink
  };
  // Corridor hues do NOT step with the theme: the basemap is `basic/main`, which
  // is light in both. Only the casing follows the page ground.
  const ALT_LAYERS = [
    { source: "route-alt1", color: CORRIDOR.alt1 },
    { source: "route-alt2", color: CORRIDOR.alt2 },
  ];
  // alt_index → identity, for everything that is not a line: the pill on the
  // line and the row in the NOW card. Indexed by alt_index and never by array
  // position — a corridor with no live sample is ABSENT from the payload list,
  // so alts[1] can be corridor 2, and painting it purple would put a pill and a
  // swatch on a line that is a different colour.
  const CORRIDOR_BY_ALT = [CORRIDOR.primary, CORRIDOR.alt1, CORRIDOR.alt2];
  // The map legend's own words, reused verbatim. A second name for the same line
  // is a second thing for the reader to learn; the swatch separates the two
  // alternates from each other.
  const CORRIDOR_NAME = ["your corridor", "alternate", "alternate"];
  const darkMap = () => document.documentElement.dataset.theme !== "light";
  // Casing = a wider line of the page ground drawn UNDER each stroke. It's what
  // makes flat lines sit ON TOP of a busy basemap instead of dissolving into it.
  const casingColor = () => (darkMap() ? "#0f1020" : "#ffffff");
  // The map is the page's LEAST important widget and its most fragile
  // dependency (WebGL can be unavailable — Lockdown Mode, blocklisted GPU, a
  // headless check). Constructing it unguarded took the ENTIRE dashboard down:
  // the throw aborted the rest of this IIFE, so the route selector rendered
  // empty and every chart stayed blank. Data first, map best-effort.
  let map = null;
  try {
    map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      sources: {
        base: { type: "raster", tiles: ["/tiles/base/{z}/{x}/{y}.png"], tileSize: 256,
                attribution: "© TomTom" },
        flow: { type: "raster", tiles: ["/tiles/flow/{z}/{x}/{y}.png"], tileSize: 256 },
      },
      layers: [
        { id: "base", type: "raster", source: "base" },
        { id: "flow", type: "raster", source: "flow", paint: { "raster-opacity": 0.7 } },
      ],
    },
      // No hardcoded center: fitBounds() frames the map from the union bbox of
      // whatever this deployment's routes actually are, as soon as the first
      // payload lands. A literal here would open every other deployment on
      // someone else's city.
      attributionControl: { compact: true },
    });
  } catch (err) {
    console.error("map unavailable", err);
    const box = document.getElementById("map");
    if (box) {
      box.classList.add("map--unavailable");
      box.textContent = "Map unavailable in this browser — every number on this page still works.";
    }
  }
  let mapReady = false;
  if (map) {
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }));
  map.on("load", () => {
    map.addSource("route", { type: "geojson", data: EMPTY_FC });
    map.addLayer({
      id: "route-casing", type: "line", source: "route",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-width": 10, "line-color": casingColor() },
    });
    map.addLayer({
      id: "route-line", type: "line", source: "route",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-width": 6,
        // Hue carries identity, so the corridor stays ONE blue end to end —
        // except where it is genuinely slow, which wears the congestion red the
        // rest of the map already speaks. Two states, not a rainbow: the flow
        // raster still owns the full ramp for every road we don't draw.
        // `mag` is TomTom's magnitudeOfDelay; >= 2 is "moderate" and worse.
        "line-color": ["case", [">=", ["coalesce", ["get", "mag"], 0], 2],
                       CORRIDOR.slow, CORRIDOR.primary],
      },
    });
    ensureAltLayers();
    // Symbols last, and in this order: nothing may cover the trip's two ends.
    ensureLabelLayer();
    ensureEndpointLayer();
    mapReady = true;
    refreshRoute();   // draw into the now-ready layers (data already rendered)
  });
  map.on("error", (e) => console.error("map error", e && e.error));
  }

  function ensureAltLayers() {
    // Idempotent: each source/layer is added at most once. Alternates are
    // inserted BELOW "route-line" so the congestion-colored primary always
    // paints on top of them.
    for (const a of ALT_LAYERS) {
      if (!map.getSource(a.source))
        map.addSource(a.source, { type: "geojson", data: EMPTY_FC });
      const before = map.getLayer("route-casing") ? "route-casing" : undefined;
      if (!map.getLayer(a.source + "-casing"))
        map.addLayer({
          id: a.source + "-casing", type: "line", source: a.source,
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": casingColor(), "line-width": 7 },
        }, before);
      if (!map.getLayer(a.source + "-line"))
        map.addLayer({
          id: a.source + "-line", type: "line", source: a.source,
          layout: { "line-cap": "round", "line-join": "round" },
          // Full opacity: translucent thin lines read as basemap noise, not
          // data. Identity is hue; hierarchy is width.
          paint: { "line-color": a.color, "line-width": 4 },
        }, before);
    }
  }

  /* ---- corridor pills + trip endpoints --------------------------------- */
  /* Both ride on MapLibre SYMBOL layers, the only thing on this map that
     collides-and-hides: two corridors that separate by half a block would
     otherwise stack two unreadable pills on the same pixel, which is worse than
     showing one time.
     The minutes are BAKED INTO A CANVAS ICON instead of drawn with `text-field`
     because text symbols need a `glyphs` PBF endpoint, and this style has none
     on purpose — every font here is a self-hosted woff2 and nothing may reach a
     CDN. Icons need no glyphs and collide exactly the same way. */
  // Same shape as the corridors: one source, layers named for what they draw.
  const LABEL_SOURCE = "route-labels", LABEL_LAYER = "route-labels-pill";
  const ENDPOINT_SOURCE = "route-endpoints", ENDPOINT_LAYER = "route-endpoints-icon";
  const ENDPOINT_ICON = { home: "ct-home", office: "ct-office" };
  // Not a corridor hue and not a theme token: the identity arc is spoken for,
  // and one fixed ink reads on the basemap, which is light under both themes.
  const ENDPOINT_INK = "#1a1b2e";
  const PILL_FONT = '700 12px "Inter Tight", system-ui, sans-serif';
  const measureCtx = document.createElement("canvas").getContext("2d");
  // Every pill is redrawn on every refresh, so cap the ratio — a 3x phone would
  // otherwise bake nine times the pixels for a 40px badge nobody zooms into.
  const iconDPR = () => Math.min(2, Math.max(1, Math.round(window.devicePixelRatio || 1)));

  function iconCanvas(w, h, dpr) {
    const c = document.createElement("canvas");
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
    const g = c.getContext("2d");
    g.scale(dpr, dpr);   // draw in CSS px; getImageData still reads device px
    return g;
  }

  function putIcon(id, g, dpr) {
    // addImage throws on a duplicate id and updateImage refuses a size change —
    // and a pill's width follows its text — so replace outright. Same tick as
    // the setData below, so no frame renders with the image missing.
    if (map.hasImage(id)) map.removeImage(id);
    map.addImage(id, g.getImageData(0, 0, g.canvas.width, g.canvas.height), { pixelRatio: dpr });
  }

  function pillPath(g, x, y, w, h) {
    const r = h / 2;
    g.beginPath();
    g.moveTo(x + r, y);
    g.lineTo(x + w - r, y);
    g.arc(x + w - r, y + r, r, -Math.PI / 2, Math.PI / 2);
    g.lineTo(x + r, y + h);
    g.arc(x + r, y + r, r, Math.PI / 2, -Math.PI / 2);
    g.closePath();
  }

  function addPillIcon(id, text, fill) {
    const dpr = iconDPR(), ring = 1.5, h = 21;
    measureCtx.font = PILL_FONT;
    const w = Math.ceil(measureCtx.measureText(text).width) + 18;
    const g = iconCanvas(w + ring * 2, h + ring * 2, dpr);
    pillPath(g, ring, ring, w, h);
    g.fillStyle = fill;
    g.fill();
    // Same job as the line casings: a ring of the page ground is what stops a
    // flat shape from dissolving into a busy basemap.
    g.lineWidth = ring * 2;
    g.strokeStyle = casingColor();
    g.stroke();
    g.font = PILL_FONT;
    g.textAlign = "center";
    g.textBaseline = "middle";
    // The per-fill contrast pick the heatmap already makes. White clears 4.5:1
    // on three of these hues; on the pink alternate it is 3.4:1, so that one pill
    // takes ink instead. Identity stays in the FILL either way.
    g.fillStyle = luminance(fill) > 0.18 ? "#1a1b2e" : "#ffffff";
    g.fillText(text, ring + w / 2, ring + h / 2 + 0.5);
    putIcon(id, g, dpr);
  }

  // Hand-drawn silhouettes on a 24x24 grid, mirroring the SF Symbols the iOS map
  // uses (house.fill / building.2.fill) — one product, one pair of shapes. Punch
  // the door and the windows back out in the badge ink so the glyph still reads
  // as a house and a pair of towers at 17px.
  function houseGlyph(g) {
    g.beginPath();
    g.moveTo(12, 2.5); g.lineTo(23, 11); g.lineTo(1, 11); g.closePath();
    g.fill();
    g.fillRect(4, 10.5, 16, 11);
    g.fillStyle = ENDPOINT_INK;
    g.fillRect(9.5, 15, 5, 6.5);
  }

  function officeGlyph(g) {
    g.fillRect(2, 9, 9, 12.5);
    g.fillRect(12.5, 2.5, 9.5, 19);
    g.fillStyle = ENDPOINT_INK;
    for (const y of [5, 8.5, 12, 15.5]) g.fillRect(14.5, y, 5.5, 1.8);
    for (const y of [11.5, 15]) g.fillRect(4, y, 5, 1.8);
  }

  function addEndpointIcon(kind) {
    const dpr = iconDPR(), ring = 2, d = 26, s = d + ring * 2, c = s / 2, box = d * 0.66;
    const g = iconCanvas(s, s, dpr);
    g.beginPath();
    g.arc(c, c, d / 2, 0, Math.PI * 2);
    g.fillStyle = ENDPOINT_INK;
    g.fill();
    g.lineWidth = ring;
    g.strokeStyle = casingColor();
    g.stroke();
    g.save();
    g.translate(c - box / 2, c - box / 2);
    g.scale(box / 24, box / 24);
    g.fillStyle = "#ffffff";
    (kind === "home" ? houseGlyph : officeGlyph)(g);
    g.restore();
    putIcon(ENDPOINT_ICON[kind], g, dpr);
  }

  function ensureLabelLayer() {
    if (!map.getSource(LABEL_SOURCE))
      map.addSource(LABEL_SOURCE, { type: "geojson", data: EMPTY_FC });
    if (map.getLayer(LABEL_LAYER)) return;
    map.addLayer({
      id: LABEL_LAYER, type: "symbol", source: LABEL_SOURCE,
      layout: {
        "icon-image": ["get", "icon"],
        // ONE layer for all three pills on purpose: sort-key priority is defined
        // WITHIN a layer, and "the primary always wins" has to be a guarantee,
        // not whatever order the style happens to place two layers in. Ascending,
        // lowest placed first — so alt_index doubles as the priority, and an
        // alternate is the one that gets dropped. Leaving icon-allow-overlap at
        // its default (false) IS the collision handling.
        "symbol-sort-key": ["get", "alt"],
        "icon-anchor": "center",
      },
    });
  }

  function ensureEndpointLayer() {
    if (!map.getSource(ENDPOINT_SOURCE))
      map.addSource(ENDPOINT_SOURCE, { type: "geojson", data: EMPTY_FC });
    for (const kind of Object.keys(ENDPOINT_ICON)) addEndpointIcon(kind);
    if (map.getLayer(ENDPOINT_LAYER)) return;
    map.addLayer({
      id: ENDPOINT_LAYER, type: "symbol", source: ENDPOINT_SOURCE,
      layout: {
        "icon-image": ["get", "icon"],
        // The two ends are anchors, not annotations: they never yield. They do
        // stay in the collision index, so a pill that lands on one is what gets
        // dropped.
        "icon-allow-overlap": true,
        "icon-anchor": "center",
      },
    });
  }

  // Where a pill goes: halfway along the line BY DISTANCE, because the corridors
  // share both endpoints and only separate in the middle.
  //
  // features[] is NOT in path order — _geojson_payload emits the congested
  // sections first and the clean stretches after — so concatenating it as given
  // measures jumps between disjoint pieces and drops the label anywhere. The
  // pieces are contiguous slices of ONE polyline, so rechain them first: start
  // at the slice end that has no neighbour (a route end) and always take the
  // nearest unused slice. Which end you start from doesn't matter; halfway is
  // halfway from either.
  function chainSlices(parts) {
    const d2 = (a, b) => (a[0] - b[0]) * (a[0] - b[0]) + (a[1] - b[1]) * (a[1] - b[1]);
    const ends = [];   // [sliceIndex, enteredAtTail, coord]
    parts.forEach((c, i) => ends.push([i, false, c[0]], [i, true, c[c.length - 1]]));
    let head = ends[0], widest = -1;
    for (const e of ends) {
      let nearest = Infinity;
      for (const o of ends) if (o[0] !== e[0]) nearest = Math.min(nearest, d2(e[2], o[2]));
      if (nearest > widest) { widest = nearest; head = e; }
    }
    const used = new Set();
    const out = [];
    while (head) {
      used.add(head[0]);
      const slice = parts[head[0]];
      for (const p of (head[1] ? slice.slice().reverse() : slice)) out.push(p);
      const tail = out[out.length - 1];
      let next = null, best = Infinity;
      for (const e of ends) {
        if (used.has(e[0])) continue;
        const dd = d2(tail, e[2]);
        if (dd < best) { best = dd; next = e; }
      }
      head = next;
    }
    return out;
  }

  function pathMidpoint(features) {
    const parts = [];
    for (const f of features) {
      const c = f.geometry && f.geometry.coordinates;
      if (c && c.length > 1) parts.push(c);
    }
    if (!parts.length) return null;
    const pts = parts.length === 1 ? parts[0] : chainSlices(parts);
    // Planar is plenty at commute scale; the cos() keeps a degree of longitude
    // honest against a degree of latitude so the halfway point isn't skewed.
    const cum = [];
    let total = 0;
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1], b = pts[i];
      const k = Math.cos((a[1] + b[1]) * Math.PI / 360);
      total += Math.hypot((b[0] - a[0]) * k, b[1] - a[1]);
      cum.push(total);
    }
    if (!total) return pts[0];
    const half = total / 2;
    let i = 0;
    while (i < cum.length - 1 && cum[i] < half) i++;
    const prev = i ? cum[i - 1] : 0;
    const t = (half - prev) / ((cum[i] - prev) || 1);
    const a = pts[i], b = pts[i + 1];
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  }

  let lastAlts = [];   // per-alt_index payloads, kept for a theme/font repaint

  function renderRouteLabels(bySlot) {
    lastAlts = bySlot;
    if (!map || !mapReady || !map.getSource(LABEL_SOURCE)) return;
    const feats = [];
    bySlot.forEach((p, i) => {
      const s = p.summary;
      // No sample, or a sample with no live number → no pill. Never "0 min",
      // never "null min": an absent time is not a fast corridor.
      if (!s || !(s.travel_sec > 0)) return;
      const at = pathMidpoint(p.features || []);
      if (!at) return;
      const icon = "ct-pill-" + i;
      addPillIcon(icon, fmtMin(s.travel_sec), CORRIDOR_BY_ALT[i]);
      feats.push({ type: "Feature", properties: { icon, alt: i },
                   geometry: { type: "Point", coordinates: at } });
    });
    map.getSource(LABEL_SOURCE).setData({ type: "FeatureCollection", features: feats });
  }

  function renderEndpoints() {
    if (!map || !mapReady || !map.getSource(ENDPOINT_SOURCE)) return;
    const r = currentRoute() || {};
    const feats = [];
    for (const place of [r.origin, r.dest]) {
      // `kind` is derived SERVER-side precisely so the browser and the phone
      // can't disagree about which end wears which icon — never re-derive it
      // from the place key here.
      const icon = place && ENDPOINT_ICON[place.kind];
      if (!icon || place.lat == null || place.lon == null) continue;
      feats.push({ type: "Feature", properties: { icon },
                   geometry: { type: "Point", coordinates: [place.lon, place.lat] } });
    }
    map.getSource(ENDPOINT_SOURCE).setData({ type: "FeatureCollection", features: feats });
  }

  function refreshRoute() {
    const r = currentRoute();
    if (!r) return;
    renderEndpoints();   // the trip's two ends are config, not poll output
    // NOT gated on mapReady: the live number and the segment list are the
    // point of this fetch; drawing is a side effect that may never happen.
    getJSON("/api/route?route=" + encodeURIComponent(r.slug) + "&alts=1").then(d => {
      // Slot by alt_index, not by array position: a corridor with no live sample
      // is ABSENT from the list rather than null, so alts[1] can be corridor 2 —
      // which the old indexing drew in corridor 1's purple.
      const bySlot = [];
      (d.alts || []).forEach((p, i) => {
        const k = p.alt_index == null ? i : p.alt_index;
        if (k >= 0 && k < CORRIDOR_BY_ALT.length) bySlot[k] = p;
      });
      const primary = bySlot[0] || { features: [] };   // slot 0 carries the congestion sections
      renderNow(primary);
      renderSegments(primary);
      renderAlternates(bySlot);
      renderRouteLabels(bySlot);   // caches even with no map: the theme repaint reuses it
      if (!map || !mapReady) return;
      map.getSource("route").setData({ type: "FeatureCollection", features: primary.features || [] });
      ALT_LAYERS.forEach((a, i) => {
        const p = bySlot[i + 1];   // no payload in that slot → clear the leftover corridor
        map.getSource(a.source).setData(p ? { type: "FeatureCollection", features: p.features || [] } : EMPTY_FC);
      });
      // union bbox from the response frames every corridor at once. The padding
      // is asymmetric because the endpoints now sit AT the bbox corners: a flat
      // 40 parks the office badge under the zoom control.
      if (d.bbox) map.fitBounds([[d.bbox[0], d.bbox[1]], [d.bbox[2], d.bbox[3]]],
        { padding: { top: 48, right: 56, bottom: 40, left: 40 }, duration: 400 });
    }).catch(err => {
      console.error(err);
      const meta = document.getElementById("now-meta");
      if (meta) meta.textContent = "couldn't reach the server — this number may be stale";
    });
  }

  function renderNow(d) {
    const s = d.summary;
    const v = document.getElementById("now-value");
    const delta = document.getElementById("now-delta");
    const meta = document.getElementById("now-meta");
    if (!s || s.travel_sec == null) {
      v.innerHTML = "—<small> min</small>";
      meta.textContent = "no live sample yet — polls run weekdays 7 AM–7 PM";
      delta.hidden = true;
      return;
    }
    v.innerHTML = min(s.travel_sec) + "<small> min</small>";
    const ref = s.historic_sec || s.free_flow_sec;
    if (ref) {
      const dm = min(s.travel_sec) - min(ref);
      delta.hidden = false;
      delta.className = "delta " + (dm > 2 ? "delta--bad" : "delta--good");
      delta.textContent = dm > 2 ? "+" + dm + " min slower than usual"
                        : dm < -2 ? Math.abs(dm) + " min faster than usual"
                        : "about usual";
    } else delta.hidden = true;
    const age = s.ts_utc ? Math.round((Date.now() - Date.parse(s.ts_utc)) / 60000) : null;
    meta.textContent = (currentRoute() || {}).label + (age != null ? " · updated " + age + " min ago" : "")
                     + (d.stale ? " · stale" : "");
    if (s.ts_utc) renderConfidence(s.ts_utc);   // a fresh poll ages the strip down
  }

  function renderSegments(d) {
    const el = document.getElementById("seglist");
    const s = d.summary;
    if (!s) { el.innerHTML = '<span class="muted">no data</span>'; return; }
    // NOTE: no "Live" row — that number is the big figure directly above. One
    // number, one place on the page.
    const rows = [];
    if (s.historic_sec != null) rows.push(["Typical (this time)", fmtMin(s.historic_sec)]);
    if (s.free_flow_sec != null) rows.push(["Free-flow", fmtMin(s.free_flow_sec)]);
    if (s.delay_sec != null) rows.push(["Delay", "+" + fmtMin(s.delay_sec)]);
    const mags = (d.features || []).filter(f => (f.properties.mag || 0) >= 2).length;
    if (mags) rows.push(["Congested stretches", String(mags)]);
    el.innerHTML = rows.length
      ? rows.map(([k, val]) =>
          `<div class="seglist__row"><span class="muted">${k}</span><span class="seglist__delay">${val}</span></div>`).join("")
      : '<span class="muted">no reference times in this sample</span>';
  }

  /* The map says WHERE the corridors go; this says what each one costs right
     now. The primary IS restated here, unlike the reference rows above — a
     comparison with its baseline missing makes the reader do the subtraction in
     their head, and the swatch is what ties a number to a line on the map. */
  function renderAlternates(bySlot) {
    const el = document.getElementById("altlist");
    if (!el) return;
    const base = bySlot[0] && bySlot[0].summary ? bySlot[0].summary.travel_sec : null;
    const rows = [];
    bySlot.forEach((p, i) => {
      const s = p.summary;
      if (!s || !(s.travel_sec > 0)) return;
      // One decimal, matching iOS and the reliability rows — whole miles would
      // collapse two corridors that differ by half a mile into the same number.
      const mi = p.length_meters ? ' <span class="muted">· ~' + (p.length_meters / 1609.34).toFixed(1) + " mi</span>" : "";
      // No primary sample → no baseline, so no gap. "same" against a number that
      // isn't there would be a claim we can't make.
      const dm = (i && base) ? min(s.travel_sec) - min(base) : null;
      // Only a FASTER alternate is coloured: it's the one that changes what you
      // do. "Three minutes worse than the road you're already on" is context.
      // Wording and units match iOS deltaText exactly. No " min" suffix: the
      // absolute figure sits in the same row already, so repeating the unit is
      // noise on the narrower surface.
      const gap = dm == null ? "" : dm === 0 ? "even" : (dm > 0 ? "+" : "−") + Math.abs(dm);
      rows.push('<div class="altlist__row">' +
        // Colour comes from CORRIDOR, never a CSS class, so a row cannot drift
        // out of agreement with the line it names.
        `<span class="legend__swatch" style="background:${CORRIDOR_BY_ALT[i]}"></span>` +
        `<span class="altlist__name">${CORRIDOR_NAME[i]}${mi}</span>` +
        (p.stale ? '<span class="nchip">stale</span>' : "") +
        `<span class="altlist__val">${fmtMin(s.travel_sec)}</span>` +
        `<span class="altlist__gap${dm < 0 ? " d-good" : " muted"}">${gap}</span></div>`);
    });
    // One corridor is not a comparison — the big number above already said it.
    el.innerHTML = rows.length > 1
      ? '<div class="altlist__title">Corridors now</div>' + rows.join("") : "";
    el.hidden = rows.length < 2;
  }

  /* ---- corridor reliability -------------------------------------------- */
  // The endpoint keys by route slug, for every enabled corridor — so this asks
  // for the one actually on screen rather than assuming the picker is on the
  // primary. Reliability follows the route selector, not just the direction.
  function renderReliability(data) {
    const r = currentRoute();
    const rel = (r && data && data[r.slug]) || { corridors: [], steadiest: null };
    const list = document.getElementById("rellist");
    const any = rel.corridors.length > 0;
    document.getElementById("rel-empty").hidden = any;
    if (!any) { list.innerHTML = ""; return; }
    list.innerHTML = rel.corridors.map((c, i) => {
      const thin = c.n < 8 ? " rellist__row--thin" : "";
      const steady = i === rel.steadiest
        ? '<span class="rellist__steady">steadiest</span>' : "";
      return `<div class="rellist__row${thin}">` +
        `<div class="rellist__head"><span class="rellist__mi">~${c.mi} mi</span>` +
        `<span class="nchip">n=${c.n}</span>${steady}</div>` +
        `<div class="rellist__stats"><span>median ${fmtMin(c.p50_sec)}</span>` +
        `<span>p90 ${fmtMin(c.p90_sec)}</span>` +
        `<span>+${fmtMin(c.buffer_sec)} buffer</span></div></div>`;
    }).join("");
  }

  function refreshReliability() {
    getJSON("/api/stats/reliability").then(renderReliability).catch(console.error);
  }

  /* ---- collection health ------------------------------------------------ */
  /* The project's named fear is "did the collection quietly stop?" — so the
     page says, out loud and always: how much data, how fresh, how much budget
     is left. Counts are server-rendered; the AGE is recomputed here so a tab
     left open overnight can't show a comforting "3 min ago". */
  const strip = document.getElementById("confidence-strip");
  let newestTs = (strip && strip.dataset.newest) || null;

  function ageText(ms) {
    const m = Math.max(0, Math.round(ms / 60000));
    if (m < 60) return m + " min ago";
    const h = Math.round(m / 60);
    if (h < 48) return h + " h ago";
    return Math.round(h / 24) + " days ago";
  }

  function renderConfidence(tsUtc) {
    if (!strip) return;
    if (tsUtc && (!newestTs || Date.parse(tsUtc) > Date.parse(newestTs))) newestTs = tsUtc;
    const n = Number(strip.dataset.samples || 0);
    const calls = Number(strip.dataset.calls || 0);
    const cap = Number(strip.dataset.cap || 0);
    const ageMs = newestTs ? Date.now() - Date.parse(newestTs) : null;
    const parts = [
      n ? n.toLocaleString() + " live samples" : "no live samples yet",
      ageMs == null ? "no poll on record" : "newest " + ageText(ageMs),
      cap ? "API budget " + calls.toLocaleString() + " / " + cap.toLocaleString() + " this month"
          : "API budget unset",
    ];
    // Only "quiet failure" INSIDE the polling window is alarming; the poller is
    // supposed to be silent at 3 AM and on Sundays. The window is evaluated
    // SERVER-side (America/New_York, from the live settings) — reading the
    // viewer's clock meant a browser in another timezone never flagged a dead
    // poller, defeating the one thing this strip exists to catch.
    const inWindow = strip.dataset.inWindow === "1";
    const stale = ageMs == null || (inWindow && ageMs > 3 * 3600 * 1000);
    strip.classList.toggle("confidence-strip--stale", stale);
    strip.innerHTML = '<span class="cs__dot" aria-hidden="true"></span>' +
      parts.map(p => `<span>${p}</span>`).join('<span class="cs__sep" aria-hidden="true">·</span>');
    strip.hidden = false;
  }

  function showNoRouteState() {
    // No routes configured yet (fresh install) — show honest empty frames
    // instead of axis skeletons that imply data is coming.
    for (const p of HOURLY_PANELS) emptyHourlyPanel(p);
    lastHeatmap = null; lastDow = null;
    document.getElementById("heatmap").innerHTML = "";
    document.getElementById("heat-legend").innerHTML = "";
    renderReliability(null);
  }

  function refreshAll() {
    // Direction-scoped only. The PLANNING zone is direction-independent, so its
    // two panels are fetched once at boot and never churn on a dir toggle.
    if (!currentRoute()) { showNoRouteState(); return; }
    refreshSpread(); refreshHeatmap(); refreshRoute(); refreshReliability();
  }

  /* auto-refresh the live pieces; pause when hidden */
  let timer = setInterval(() => refreshRoute(), 120000);
  document.addEventListener("visibilitychange", () => {
    clearInterval(timer);
    if (!document.hidden) { refreshRoute(); timer = setInterval(() => refreshRoute(), 120000); }
  });

  // A pill bakes whatever face was loaded at the moment it was drawn, and the
  // first refresh can beat the woff2 in. Redraw once the real one lands.
  if (document.fonts) document.fonts.ready.then(() => renderRouteLabels(lastAlts));

  renderControls();
  renderConfidence(null);
  refreshHourly();     // PLANNING: both half-days, independent of the switcher
  if (!currentRoute()) showNoRouteState();
  else refreshAll();   // includes refreshRoute: the live number never waits for the map
})();
