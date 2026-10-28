// Run with `bun test tests/web/js` (tests/web/test_js.py runs it under pytest when bun is installed).
import { beforeAll, describe, expect, test } from "bun:test";

beforeAll(async () => {
  await import("../../../src/papi/web/static/livestream.js");
  await import("../../../src/papi/web/static/feedorder.js");
});

class FakeTarget {
  constructor() { this.listeners = {}; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, ev = {}) { (this.listeners[type] || []).forEach((fn) => fn(ev)); }
}

// Manual clock: timers fire only when the test advances time.
class Clock {
  constructor() { this.t = 0; this.timers = new Map(); this.seq = 0; }
  setTimeout = (fn, ms) => { const id = ++this.seq; this.timers.set(id, { at: this.t + ms, fn }); return id; };
  clearTimeout = (id) => { this.timers.delete(id); };
  now = () => this.t;
  pendingDelays() { return [...this.timers.values()].map((x) => x.at - this.t); }
  async advance(ms) {
    const end = this.t + ms;
    for (;;) {
      const due = [...this.timers.entries()].filter(([, x]) => x.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      this.timers.delete(due[0]);
      this.t = due[1].at;
      due[1].fn();
      await flushMicrotasks();
    }
    this.t = end;
    await flushMicrotasks();
  }
}

async function flushMicrotasks() { for (let i = 0; i < 10; i++) await Promise.resolve(); }

class FakeAbort {
  constructor() { this.signal = { aborted: false }; }
  abort() { this.signal.aborted = true; }
}

// fetch that resolves only when the test says so; records every request.
function fakeFetch() {
  const calls = [];
  const fn = (url, init) => new Promise((resolve, reject) => {
    calls.push({ url, init, resolve: (body, ok = true) => resolve({ ok, status: ok ? 200 : 500, json: async () => body }), reject });
  });
  fn.calls = calls;
  return fn;
}

function harness(cursor = "c0") {
  const clock = new Clock();
  const fetch = fakeFetch();
  const got = [];
  const poller = globalThis.papiLive.createPoller({
    url: "/stream/poll?scope=all",
    cursor,
    onItems: (items) => got.push(...items.map((i) => i.id)),
    fetch,
    AbortController: FakeAbort,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    now: clock.now,
    random: () => 0.5,
  });
  const win = new FakeTarget();
  const doc = new FakeTarget();
  doc.visibilityState = "visible";
  return { clock, fetch, got, poller, win, doc };
}

describe("poll loop", () => {
  test("chains requests without overlap and follows the cursor", async () => {
    const h = harness();
    h.poller.start();
    await h.clock.advance(0);
    expect(h.fetch.calls.length).toBe(1);
    expect(h.fetch.calls[0].url).toBe("/stream/poll?scope=all&after=c0");
    expect(h.fetch.calls[0].init.cache).toBe("no-store");
    await h.clock.advance(60000);  // a slow response: no second request meanwhile
    expect(h.fetch.calls.length).toBe(1);
    h.fetch.calls[0].resolve({ cursor: "c1", items: [{ id: "a" }], more: false });
    await flushMicrotasks();
    expect(h.got).toEqual(["a"]);
    expect(h.clock.pendingDelays()).toEqual([globalThis.papiLive.INTERVAL_MS]);
    await h.clock.advance(2499);
    expect(h.fetch.calls.length).toBe(1);
    await h.clock.advance(1);
    expect(h.fetch.calls.length).toBe(2);
    expect(h.fetch.calls[1].url).toBe("/stream/poll?scope=all&after=c1");
    h.fetch.calls[1].resolve({ cursor: "c2", items: [{ id: "b" }], more: true });  // capped: fetch again at once
    await flushMicrotasks();
    expect(h.clock.pendingDelays()).toEqual([0]);
    await h.clock.advance(0);
    expect(h.fetch.calls[2].url).toBe("/stream/poll?scope=all&after=c2");
    expect(h.poller.healthy()).toBe(true);
  });

  test("backs off to 10 s plus jitter after errors, then recovers", async () => {
    const h = harness();
    h.poller.start();
    await h.clock.advance(0);
    h.fetch.calls[0].resolve({}, false);  // HTTP 500
    await flushMicrotasks();
    expect(h.clock.pendingDelays()).toEqual([10000 + 500]);
    await h.clock.advance(10500);
    expect(h.fetch.calls.length).toBe(2);
    expect(h.fetch.calls[1].url).toBe("/stream/poll?scope=all&after=c0");  // cursor kept
    h.fetch.calls[1].reject(new Error("network"));
    await flushMicrotasks();
    expect(h.clock.pendingDelays()).toEqual([10500]);
    await h.clock.advance(10500);
    h.fetch.calls[2].resolve({ cursor: "c1", items: [], more: false });
    await flushMicrotasks();
    expect(h.clock.pendingDelays()).toEqual([2500]);
    expect(h.poller.healthy()).toBe(true);
    await h.clock.advance(20000);
    // the next poll goes out 2.5 s later (and waits for its response: no overlap)
    expect(h.fetch.calls.length).toBe(3 + 1);
  });

  test("stop aborts the in-flight request; a late response is ignored", async () => {
    const h = harness();
    h.poller.start();
    await h.clock.advance(0);
    const first = h.fetch.calls[0];
    expect(h.poller.stop()).toBe(true);
    expect(first.init.signal.aborted).toBe(true);
    expect(h.clock.pendingDelays()).toEqual([]);
    first.resolve({ cursor: "late", items: [{ id: "late" }], more: false });
    await flushMicrotasks();
    expect(h.got).toEqual([]);
    expect(h.poller.cursor()).toBe("c0");
    h.poller.start();
    await h.clock.advance(0);
    expect(h.fetch.calls.length).toBe(2);
    expect(h.poller.inFlight()).toBe(true);
  });

  test("lifecycle: hidden / pagehide stop, visible / pageshow poll immediately, navigation aborts", async () => {
    const h = harness();
    globalThis.papiLive.bindLifecycle(h.poller, h.win, h.doc, (ev) => ev.inApp === true, h.clock.setTimeout);
    await h.clock.advance(0);
    expect(h.fetch.calls.length).toBe(1);
    h.fetch.calls[0].resolve({ cursor: "c1", items: [], more: false });
    await flushMicrotasks();

    h.doc.visibilityState = "hidden";
    h.doc.dispatch("visibilitychange");
    expect(h.poller.isRunning()).toBe(false);
    await h.clock.advance(60000);
    expect(h.fetch.calls.length).toBe(1);  // nothing while hidden
    h.win.dispatch("pageshow", { persisted: true });  // still hidden: stay stopped
    expect(h.poller.isRunning()).toBe(false);
    h.doc.visibilityState = "visible";
    h.doc.dispatch("visibilitychange");
    await h.clock.advance(0);
    expect(h.fetch.calls.length).toBe(2);
    expect(h.fetch.calls[1].url).toBe("/stream/poll?scope=all&after=c1");  // stored cursor

    h.win.dispatch("pagehide", { persisted: true });
    expect(h.fetch.calls[1].init.signal.aborted).toBe(true);
    h.win.dispatch("pageshow", { persisted: true });
    await h.clock.advance(0);
    expect(h.fetch.calls.length).toBe(3);

    h.doc.dispatch("click", { inApp: false });
    expect(h.fetch.calls[2].init.signal.aborted).toBe(false);
    h.doc.dispatch("click", { inApp: true });
    expect(h.fetch.calls[2].init.signal.aborted).toBe(true);
    expect(h.poller.isRunning()).toBe(false);
    await h.clock.advance(5000);  // navigation did not happen: resume
    expect(h.fetch.calls.length).toBe(4);
    h.doc.dispatch("submit");
    expect(h.fetch.calls[3].init.signal.aborted).toBe(true);
    h.win.dispatch("beforeunload");
    expect(h.poller.isRunning()).toBe(false);
    // never more than one request in flight
    expect(h.fetch.calls.filter((c) => !c.init.signal.aborted).length).toBeLessThanOrEqual(1);
  });

  test("in-app click detection", () => {
    const link = (href, attrs = {}) => ({
      href: href.startsWith("http") ? href : "http://127.0.0.1:8765" + href,
      target: attrs.target || "",
      getAttribute: () => href,
      hasAttribute: (n) => n in attrs,
    });
    const ev = (a, extra = {}) => ({ button: 0, target: { closest: () => a }, ...extra });
    const nav = globalThis.papiLive.isInAppNavClick;
    const origin = "http://127.0.0.1:8765";
    expect(nav(ev(link("/events/x")), origin)).toBe(true);
    expect(nav(ev(link("/events/x"), { metaKey: true }), origin)).toBe(false);
    expect(nav(ev(link("https://www.kap.org.tr/tr/Bildirim/1")), origin)).toBe(false);
    expect(nav(ev(link("/raw/x", { target: "_blank" })), origin)).toBe(false);
    expect(nav(ev(link("#d0s1")), origin)).toBe(false);
    expect(nav(ev(null), origin)).toBe(false);
  });
});

describe("feed ordering", () => {
  const k = (published, ref, mat = 50) => ({ id: "e" + ref, published, ref, mat });
  const desc = [k("2026-10-28T06:17:00.000000Z", 30), k("2026-10-28T05:00:00.000000Z", 20), k("2026-10-27T15:40:00.000000Z", 10)];

  test("catch-up items slot in, never jump to the top", () => {
    const F = globalThis.papiFeed;
    expect(F.decide(k("2026-10-27T15:50:00.000000Z", 15), desc, "time_desc", false)).toEqual({ index: 2, isNew: false });
    expect(F.decide(k("2026-10-27T15:36:00.000000Z", 5), desc, "time_desc", true)).toEqual({ beyond: true, isNew: false });
    expect(F.decide(k("2026-10-27T15:36:00.000000Z", 5), desc, "time_desc", false)).toEqual({ index: -1, isNew: false });
    expect(F.decide(k("2026-10-28T06:30:00.000000Z", 40), desc, "time_desc", true)).toEqual({ index: 0, isNew: true });
    expect(F.decide(k("2026-10-28T05:00:00.000000Z", 25), desc, "time_desc", true)).toEqual({ index: 1, isNew: false });
  });

  test("ascending and materiality sorts", () => {
    const F = globalThis.papiFeed;
    expect(F.decide(k("2026-10-28T05:30:00.000000Z", 25), desc.slice().reverse(), "time_asc", true).index).toBe(2);
    const byMat = [k("2026-10-27T10:00:00.000000Z", 1, 90), k("2026-10-28T10:00:00.000000Z", 2, 40)];
    expect(F.decide(k("2026-10-28T11:00:00.000000Z", 3, 60), byMat, "materiality", true)).toEqual({ index: 1, isNew: true });
    expect(F.decide(k("", 3), desc, "time_desc", false).index).toBe(-1);  // no publish time sorts last
  });
});
