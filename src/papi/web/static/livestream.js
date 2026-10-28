// papi live feed: short polling over plain fetch (no long-lived connections).
//
// Long-lived EventSource connections exhausted the browser's ~6 HTTP/1.1 connections per host when pages
// sat in the back/forward cache or several tabs were open. The page now asks `/stream/poll?after=<cursor>`
// every few seconds; each request returns immediately. One request at a time (setTimeout chaining), none
// while hidden, and the in-flight request is aborted on pagehide / form submit / in-app link clicks.
(function (root) {
  "use strict";

  var INTERVAL_MS = 2500;
  var ERROR_BACKOFF_MS = 10000;
  var JITTER_MS = 1000;
  var HEALTHY_MS = 10000;

  // opts: { url, cursor, onItems(items), onState(ok),
  //         fetch, AbortController, setTimeout, clearTimeout, now, random }   (injectable for tests)
  function createPoller(opts) {
    var fetchFn = opts.fetch || root.fetch.bind(root);
    var Abort = opts.AbortController || root.AbortController;
    var setT = opts.setTimeout || root.setTimeout.bind(root);
    var clearT = opts.clearTimeout || root.clearTimeout.bind(root);
    var now = opts.now || Date.now;
    var random = opts.random || Math.random;
    var onState = opts.onState || function () {};
    var cursor = opts.cursor || null;
    var running = false;
    var timer = null;
    var ctrl = null;  // the in-flight request's controller (null when idle)
    var lastOk = 0;
    var requests = 0;

    function urlFor() {
      if (!cursor) return opts.url;
      return opts.url + (opts.url.indexOf("?") === -1 ? "?" : "&") + "after=" + encodeURIComponent(cursor);
    }

    function schedule(ms) {
      if (timer !== null) clearT(timer);
      timer = setT(tick, ms);
    }

    function finish(mine, delay) {
      if (ctrl !== mine) return;  // aborted or superseded: someone else owns the state now
      ctrl = null;
      if (running) schedule(delay);
    }

    function tick() {
      timer = null;
      if (!running || ctrl) return;  // never two requests at once
      var mine = Abort ? new Abort() : {};
      ctrl = mine;
      requests += 1;
      fetchFn(urlFor(), {
        signal: mine.signal,
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then(function (body) {
          if (ctrl !== mine) return;
          if (body.cursor) cursor = body.cursor;
          lastOk = now();
          onState(true);
          if (body.items && body.items.length) opts.onItems(body.items);
          finish(mine, body.more ? 0 : INTERVAL_MS);
        })
        .catch(function () {
          if (ctrl !== mine) return;  // aborted on purpose
          onState(false);
          finish(mine, ERROR_BACKOFF_MS + Math.floor(random() * JITTER_MS));
        });
    }

    function start() {  // poll now, then keep polling
      if (running) return false;
      running = true;
      schedule(0);
      return true;
    }

    function stop() {  // stop polling and abort the in-flight request
      var was = running || ctrl !== null;
      running = false;
      if (timer !== null) { clearT(timer); timer = null; }
      if (ctrl) {
        var c = ctrl;
        ctrl = null;
        if (c.abort) c.abort();
      }
      return was;
    }

    return {
      start: start,
      stop: stop,
      isRunning: function () { return running; },
      inFlight: function () { return ctrl !== null; },
      cursor: function () { return cursor; },
      url: urlFor,
      requests: function () { return requests; },
      healthy: function () { return lastOk > 0 && now() - lastOk < HEALTHY_MS; },
    };
  }

  // Wire `poller` to page lifecycle events of `win`/`doc`. `isInAppNav(ev)` decides whether a click
  // navigates this tab within papi. After a submit/click the poller restarts if the page is still here
  // (navigation cancelled), using `setT`.
  function bindLifecycle(poller, win, doc, isInAppNav, setT) {
    var later = setT || (win.setTimeout ? win.setTimeout.bind(win) : null);
    function sync() { if (doc.visibilityState === "hidden") poller.stop(); else poller.start(); }
    function leaving() {
      poller.stop();
      if (later) later(sync, 5000);
    }
    win.addEventListener("pagehide", function () { poller.stop(); });
    win.addEventListener("beforeunload", function () { poller.stop(); });
    win.addEventListener("pageshow", sync);  // incl. event.persisted (bfcache restore)
    doc.addEventListener("visibilitychange", sync);
    doc.addEventListener("submit", leaving, true);
    doc.addEventListener("click", function (ev) { if (isInAppNav && isInAppNav(ev)) leaving(); }, true);
    sync();
  }

  // A plain left click on a same-origin link that navigates this tab.
  function isInAppNavClick(ev, origin) {
    if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return false;
    var a = ev.target && ev.target.closest ? ev.target.closest("a[href]") : null;
    if (!a || (a.target && a.target !== "_self") || a.hasAttribute("download")) return false;
    var href = a.getAttribute("href") || "";
    if (href.charAt(0) === "#") return false;
    return a.href.indexOf(origin + "/") === 0;
  }

  root.papiLive = {
    createPoller: createPoller,
    bindLifecycle: bindLifecycle,
    isInAppNavClick: isInAppNavClick,
    INTERVAL_MS: INTERVAL_MS,
    ERROR_BACKOFF_MS: ERROR_BACKOFF_MS,
    JITTER_MS: JITTER_MS,
  };
})(typeof window !== "undefined" ? window : globalThis);
