// papi dashboard: span highlighting, live feed (SSE), keyboard navigation, uPlot charts. No external requests.
(function () {
  "use strict";

  // ---- span highlighting -------------------------------------------------------------------------
  function anchorsOf(el) { return (el.getAttribute("data-anchor") || "").split(/\s+/).filter(Boolean); }
  function highlight(ids, on) {
    ids.forEach(function (id) { var t = document.getElementById(id); if (t) t.classList.toggle("hl", on); });
  }
  document.addEventListener("mouseover", function (e) {
    var el = e.target.closest && e.target.closest("[data-anchor]"); if (el) highlight(anchorsOf(el), true);
  });
  document.addEventListener("mouseout", function (e) {
    var el = e.target.closest && e.target.closest("[data-anchor]"); if (el) highlight(anchorsOf(el), false);
  });
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-anchor]"); if (!el) return;
    var ids = anchorsOf(el), first = ids.length && document.getElementById(ids[0]);
    if (!first) return;
    e.preventDefault();
    document.querySelectorAll(".span.hl").forEach(function (s) { s.classList.remove("hl"); });
    highlight(ids, true);
    first.scrollIntoView({ behavior: "smooth", block: "center" });
    history.replaceState(null, "", "#" + ids[0]);
  });
  if (location.hash.length > 1) {
    var target = document.getElementById(location.hash.slice(1));
    if (target && target.classList.contains("span")) target.classList.add("hl");
  }

  // ---- filter form: auto-submit on change (the "Filtrele" button stays for no-JS use) --------------
  document.documentElement.classList.add("js");
  document.querySelectorAll("form[data-autosubmit]").forEach(function (form) {
    form.addEventListener("change", function () {
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    });
    form.addEventListener("submit", function () {  // keep the URL short: drop empty parameters
      Array.prototype.forEach.call(form.elements, function (el) {
        if (el.name && !el.value && el.type !== "radio" && el.type !== "checkbox") el.disabled = true;
      });
    });
  });

  // ---- live feed ---------------------------------------------------------------------------------
  // Items are placed by their publish time (data-published, see feedorder.js), never at the top just
  // because they were processed now: catch-up/backfill items slot into their place or, when they sort
  // below the rendered page, are only counted. Only items newer than everything shown animate.
  var feed = document.getElementById("feed");
  var pill = document.getElementById("newpill");
  var reloadPill = document.getElementById("reloadpill");
  var oldNote = document.getElementById("oldnote");
  var live = document.getElementById("live");
  var pending = [];
  var oldCount = 0;
  function fragment(html) { var t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; }
  function feedItems() { return Array.prototype.slice.call(feed.querySelectorAll(".item")); }
  function burstCount(details) {
    var n = details.querySelectorAll(".item").length, el = details.querySelector(".burst-n");
    if (el) el.textContent = n;
  }
  function insertBefore(node, target) {
    if (!target) { feed.appendChild(node); return; }
    var burst = target.closest("details.burst");
    if (!burst) { target.parentNode.insertBefore(node, target); return; }
    if (burst.dataset.code === node.dataset.code) { burst.insertBefore(node, target); burstCount(burst); return; }
    feed.insertBefore(node, burst);  // different code: keep the burst intact, place just above it
  }
  function place(node) {
    var old = document.getElementById(node.id);
    if (old) { old.replaceWith(node); return; }  // status update (e.g. analysed) keeps its position
    var list = feedItems(), sort = feed.dataset.sort || "time_desc";
    var d = papiFeed.decide(papiFeed.keyOf(node), list.map(papiFeed.keyOf), sort, feed.dataset.hasMore === "1");
    if (d.beyond) {
      if (sort === "time_desc") { oldCount += 1; oldNote.hidden = false; oldNote.querySelector(".n").textContent = oldCount; }
      else { reloadPill.hidden = false; }
      return;
    }
    var empty = document.getElementById("feed-empty"); if (empty) empty.remove();
    insertBefore(node, d.index === -1 ? null : list[d.index]);
    if (d.isNew) node.classList.add("fresh");
  }
  function flush() {
    pending.splice(0).forEach(place);
    pill.hidden = true;
  }
  if (feed && feed.dataset.poll && window.fetch && window.papiFeed && window.papiLive) {
    var onItem = function (item) {
      if (!item.html) return;
      var node = fragment(item.html);
      var key = papiFeed.keyOf(node), list = feedItems().map(papiFeed.keyOf);
      var d = papiFeed.decide(key, list, feed.dataset.sort || "time_desc", feed.dataset.hasMore === "1");
      // Brand-new items at the top of a newest-first feed wait behind the "N yeni" pill while scrolled.
      if (!document.getElementById(node.id) && d.isNew && d.index === 0 && window.scrollY > 200) {
        pending.push(node);
        pill.hidden = false; pill.querySelector("button").textContent = pending.length + " yeni";
        return;
      }
      place(node);
    };
    // Short polling (see livestream.js): one request at a time, none while hidden, aborted on navigation.
    var poller = papiLive.createPoller({
      url: feed.dataset.poll,
      cursor: feed.dataset.cursor,
      onItems: function (list) { list.forEach(onItem); },
      onState: function () { live.classList.toggle("on", poller.healthy()); },
    });
    setInterval(function () { live.classList.toggle("on", poller.isRunning() && poller.healthy()); }, 2500);
    papiLive.bindLifecycle(poller, window, document, function (ev) {
      return papiLive.isInAppNavClick(ev, location.origin);
    });
    pill.querySelector("button").addEventListener("click", function () { flush(); window.scrollTo({ top: 0 }); });
    window.addEventListener("scroll", function () { if (window.scrollY < 50 && pending.length) flush(); });
  }

  // ---- keyboard ----------------------------------------------------------------------------------
  var cur = -1;
  function items() { return Array.prototype.slice.call(document.querySelectorAll(".item")); }
  function focusAt(i) {
    var list = items(); if (!list.length) return;
    cur = Math.max(0, Math.min(list.length - 1, i));
    list.forEach(function (x) { x.classList.remove("focus"); });
    var el = list[cur]; el.classList.add("focus");
    var d = el.closest("details"); if (d) d.open = true;
    el.scrollIntoView({ block: "nearest" });
  }
  document.addEventListener("keydown", function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") { if (e.key === "Escape") e.target.blur(); return; }
    var list = items(), el = list[cur];
    if (e.key === "j") { focusAt(cur + 1); }
    else if (e.key === "k") { focusAt(cur - 1); }
    else if (e.key === "/") { var f = document.getElementById("f-ticker"); if (f) { e.preventDefault(); f.focus(); } }
    else if (e.key === "o" && el) { var k = el.querySelector("a.kap"); if (k) window.open(k.href, "_blank", "noopener"); }
    else if (e.key === "Enter" && el) { var h = el.querySelector("a.headline"); if (h) location.href = h.href; }
  });

  // ---- charts (uPlot) ----------------------------------------------------------------------------
  function trNum(v, d) { return v == null ? "—" : v.toLocaleString("tr-TR", { maximumFractionDigits: d }); }
  function makeChart(el) {
    var dataEl = el.nextElementSibling; if (!dataEl || !window.uPlot) return;
    var d = JSON.parse(dataEl.textContent), kind = el.dataset.kind, w = el.clientWidth || 600, opts, data;
    var tz = function (ts) { return uPlot.tzDate(new Date(ts * 1e3), "Europe/Istanbul"); };
    if (kind === "price") {
      data = [d.x, d.close, d.buy, d.sell];
      opts = { width: w, height: 260, tzDate: tz, series: [{},
        { label: "Kapanış", stroke: "#0b5cad", width: 1.5, value: function (u, v) { return trNum(v, 2); } },
        { label: "Alış", stroke: "#0a7a3d", paths: function () { return null; }, points: { show: true, size: 9, fill: "#0a7a3d" } },
        { label: "Satış", stroke: "#b3261e", paths: function () { return null; }, points: { show: true, size: 9, fill: "#b3261e" } }] };
    } else if (kind === "usage") {
      data = [d.x, d.cost];
      opts = { width: w, height: 220, tzDate: tz, series: [{},
        { label: "USD/saat", stroke: "#6b3fa0", fill: "rgba(107,63,160,.25)", paths: uPlot.paths.bars({ size: [0.9, 40] }),
          value: function (u, v) { return trNum(v, 4); } }] };
    } else {
      data = [d.x, d.y];
      opts = { width: w, height: 200, tzDate: tz, scales: { y: { distr: 3 } }, series: [{},
        { label: "Gecikme (sn)", stroke: "#9a4d00", paths: function () { return null; }, points: { show: true, size: 4, fill: "#9a4d00" } }] };
    }
    new uPlot(opts, data, el);
  }
  document.querySelectorAll(".chart").forEach(makeChart);
})();
