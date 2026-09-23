/* /trips list controls — column sorting + verdict filters.

   Both are CLIENT-side on purpose. The server already ships the whole window
   in one response, so a re-sort costs nothing and a round trip would only add
   latency; and the htmx verdict swap has to keep working against rows already
   in the DOM either way.

   The server's ORDER BY is NOT the display order and must not be "fixed" to
   match it: it decides WHICH rows make the LIMIT window — needs-review first,
   so a flagged trip can never fall off the page however old it is — and this
   file decides how that window is stacked. Default: date+time, newest first. */

(function () {
  "use strict";

  const table = document.getElementById("trips-table");
  if (!table) return;                        // no trips yet — nothing to control

  const body = document.getElementById("trips-body");
  const empty = document.getElementById("trips-empty");
  const countEl = document.getElementById("trip-count");
  const chips = Array.from(document.querySelectorAll("#trip-filters .chip"));
  const heads = Array.from(table.tHead.rows[0].cells);
  const BUCKETS = chips.map(c => c.dataset.bucket);

  const SORT_KEY = "ct.trips.sort";
  const FILTER_KEY = "ct.trips.filters";
  const DEFAULT_SORT = { col: 0, dir: "desc" };          // date+time, newest first
  const DEFAULT_FILTERS = ["pending", "confirmed"];      // rejected hidden

  let sort = readSort();
  let filters = readFilters();

  /* Only real trip rows carry data-bucket; #trips-empty deliberately doesn't,
     which keeps it out of both the sort and the counts. */
  function rows() { return Array.from(body.querySelectorAll("tr[data-bucket]")); }

  /* ---- state persistence ---------------------------------------------- */

  function readSort() {
    const raw = (localStorage.getItem(SORT_KEY) || "").split(":");
    const col = Number(raw[0]);
    const ok = (raw[1] === "asc" || raw[1] === "desc")
            && col >= 0 && col < heads.length && heads[col].dataset.type;
    return ok ? { col: col, dir: raw[1] } : Object.assign({}, DEFAULT_SORT);
  }

  function readFilters() {
    const raw = localStorage.getItem(FILTER_KEY);
    // "" is a legitimate saved state (every chip off), so only a MISSING key
    // falls back to the default.
    if (raw === null) return DEFAULT_FILTERS.slice();
    return raw.split(",").filter(b => BUCKETS.indexOf(b) !== -1);
  }

  /* ---- sorting --------------------------------------------------------- */

  function applySort() {
    const numeric = heads[sort.col].dataset.type === "num";
    const sign = sort.dir === "asc" ? 1 : -1;
    const sorted = rows().sort(function (a, b) {
      const av = a.cells[sort.col].dataset.sort;
      const bv = b.cells[sort.col].dataset.sort;
      // A missing value is unknown, not small, so it sinks in BOTH directions
      // rather than flooding the top of every ascending sort.
      if (!av && !bv) return tiebreak(a, b);
      if (!av) return 1;
      if (!bv) return -1;
      const cmp = numeric ? Number(av) - Number(bv) : av.localeCompare(bv);
      return cmp !== 0 ? cmp * sign : tiebreak(a, b);
    });
    sorted.forEach(r => body.appendChild(r));
    body.appendChild(empty);                 // the placeholder always sits last
    heads.forEach(function (th, i) {
      th.setAttribute("aria-sort",
        i !== sort.col ? "none" : sort.dir === "asc" ? "ascending" : "descending");
    });
  }

  // Equal keys fall back to newest first, so a re-sort of the Status or Verdict
  // column can't shuffle rows that tie into an arbitrary new order each time.
  function tiebreak(a, b) {
    return b.cells[0].dataset.sort.localeCompare(a.cells[0].dataset.sort);
  }

  /* ---- filtering ------------------------------------------------------- */

  function applyFilter() {
    const all = rows();
    let shown = 0;
    all.forEach(function (r) {
      const on = filters.indexOf(r.dataset.bucket) !== -1;
      r.hidden = !on;
      if (on) shown++;
    });
    empty.hidden = shown > 0;
    countEl.textContent = shown === all.length
      ? all.length + (all.length === 1 ? " trip" : " trips")
      : shown + " of " + all.length + " trips";
    chips.forEach(function (c) {
      c.setAttribute("aria-pressed", filters.indexOf(c.dataset.bucket) !== -1);
    });
  }

  /* ---- wiring ---------------------------------------------------------- */

  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      const b = chip.dataset.bucket;
      const at = filters.indexOf(b);
      if (at === -1) filters.push(b); else filters.splice(at, 1);
      localStorage.setItem(FILTER_KEY, filters.join(","));
      applyFilter();
    });
  });

  heads.forEach(function (th, i) {
    const btn = th.querySelector(".th-sort");
    if (!btn) return;
    btn.addEventListener("click", function () {
      // Re-clicking the sorted column flips it; a new column opens at whichever
      // direction reads as "most interesting first" for its kind of value.
      sort = i === sort.col
        ? { col: i, dir: sort.dir === "asc" ? "desc" : "asc" }
        : { col: i, dir: th.dataset.default };
      localStorage.setItem(SORT_KEY, sort.col + ":" + sort.dir);
      applySort();
    });
  });

  // A verdict swap replaces one <tr> with a fresh one carrying a new bucket:
  // reject a trip while Rejected is off and it should leave the list. Re-filter
  // but never re-sort — the row must not jump out from under the pointer.
  document.body.addEventListener("htmx:afterSwap", applyFilter);

  applySort();
  applyFilter();
})();
