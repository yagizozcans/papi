// papi live stream lifecycle: at most one EventSource per page, closed whenever the page is not in use.
//
// Browsers allow ~6 HTTP/1.1 connections per host. Pages kept in the back/forward cache (or hidden tabs)
// would otherwise hold their EventSource open and starve new navigations, so the stream is closed on
// pagehide / beforeunload / hidden / before in-app navigation, and reopened on pageshow / visible.
// A reopened EventSource resumes with `?after=<last event id>` (a new EventSource cannot set
// Last-Event-ID); the browser's own automatic reconnects send the Last-Event-ID header.
(function (root) {
  "use strict";

  // opts: { url, types: [event names], factory(url) -> EventSource-like, onMessage(type, ev), onState(bool) }
  function createLiveStream(opts) {
    var es = null;
    var lastId = null;
    var factory = opts.factory || function (u) { return new root.EventSource(u); };
    var onState = opts.onState || function () {};

    function urlFor() {
      if (!lastId) return opts.url;
      return opts.url + (opts.url.indexOf("?") === -1 ? "?" : "&") + "after=" + encodeURIComponent(lastId);
    }

    function open() {
      if (es) return false;  // never more than one per page
      var src = factory(urlFor());
      es = src;
      src.onopen = function () { if (es === src) onState(true); };
      src.onerror = function () { if (es === src) onState(false); };
      (opts.types || []).forEach(function (type) {
        src.addEventListener(type, function (ev) {
          if (es !== src) return;  // late event from a closed source
          if (ev.lastEventId) lastId = ev.lastEventId;
          opts.onMessage(type, ev);
        });
      });
      return true;
    }

    function close() {
      if (!es) return false;
      var src = es;
      es = null;
      src.close();
      onState(false);
      return true;
    }

    return {
      open: open,
      close: close,
      isOpen: function () { return es !== null; },
      lastId: function () { return lastId; },
      url: urlFor,
    };
  }

  // Wire `stream` to page lifecycle events of `win`/`doc`. `isInAppNav(ev)` decides whether a click
  // navigates this tab within papi (then the stream closes before the request is sent).
  function bindLifecycle(stream, win, doc, isInAppNav) {
    function sync() { if (doc.visibilityState === "hidden") stream.close(); else stream.open(); }
    win.addEventListener("pagehide", function () { stream.close(); });
    win.addEventListener("beforeunload", function () { stream.close(); });
    win.addEventListener("pageshow", function () { sync(); });  // incl. event.persisted (bfcache restore)
    doc.addEventListener("visibilitychange", sync);
    doc.addEventListener("submit", function () { stream.close(); }, true);
    doc.addEventListener("click", function (ev) { if (isInAppNav && isInAppNav(ev)) stream.close(); }, true);
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

  root.papiLive = { createLiveStream: createLiveStream, bindLifecycle: bindLifecycle, isInAppNavClick: isInAppNavClick };
})(typeof window !== "undefined" ? window : globalThis);
