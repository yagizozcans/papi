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

class FakeES extends FakeTarget {
  constructor(url) { super(); this.url = url; this.closed = false; }
  close() { this.closed = true; }
  emit(type, id, data = "{}") { this.dispatch(type, { lastEventId: id, data }); }
}

function harness() {
  const made = [];
  const got = [];
  const states = [];
  const stream = globalThis.papiLive.createLiveStream({
    url: "/stream?scope=all",
    types: ["event.classified", "papi.cursor"],
    factory: (u) => { const es = new FakeES(u); made.push(es); return es; },
    onMessage: (type, ev) => got.push([type, ev.lastEventId]),
    onState: (on) => states.push(on),
  });
  const win = new FakeTarget();
  const doc = new FakeTarget();
  doc.visibilityState = "visible";
  const open = () => made.filter((es) => !es.closed);
  return { stream, made, got, states, win, doc, open };
}

describe("live stream lifecycle", () => {
  test("opens once, resumes with ?after= after close", () => {
    const h = harness();
    expect(h.stream.open()).toBe(true);
    expect(h.stream.open()).toBe(false);  // never two per page
    expect(h.made.length).toBe(1);
    expect(h.made[0].url).toBe("/stream?scope=all");
    h.made[0].emit("event.classified", "01a-1");
    h.made[0].emit("papi.cursor", "t:2026-10-28T09:00:00.000000Z");
    expect(h.stream.close()).toBe(true);
    expect(h.made[0].closed).toBe(true);
    h.made[0].emit("event.classified", "late");  // ignored after close
    expect(h.got).toEqual([["event.classified", "01a-1"], ["papi.cursor", "t:2026-10-28T09:00:00.000000Z"]]);
    h.stream.open();
    expect(h.made[1].url).toBe("/stream?scope=all&after=t%3A2026-10-28T09%3A00%3A00.000000Z");
  });

  test("pagehide / beforeunload / hidden / submit / in-app click close; pageshow / visible reopen", () => {
    const h = harness();
    globalThis.papiLive.bindLifecycle(h.stream, h.win, h.doc, (ev) => ev.inApp === true);
    expect(h.open().length).toBe(1);

    h.win.dispatch("pagehide", { persisted: true });
    expect(h.open().length).toBe(0);
    h.win.dispatch("pageshow", { persisted: true });  // bfcache restore
    expect(h.open().length).toBe(1);

    h.doc.visibilityState = "hidden";
    h.doc.dispatch("visibilitychange");
    expect(h.open().length).toBe(0);
    h.win.dispatch("pageshow", { persisted: true });  // restored but still hidden: stay closed
    expect(h.open().length).toBe(0);
    h.doc.visibilityState = "visible";
    h.doc.dispatch("visibilitychange");
    expect(h.open().length).toBe(1);

    h.doc.dispatch("click", { inApp: false });
    expect(h.open().length).toBe(1);
    h.doc.dispatch("click", { inApp: true });
    expect(h.open().length).toBe(0);
    h.win.dispatch("pageshow", {});
    h.doc.dispatch("submit");
    expect(h.open().length).toBe(0);
    h.win.dispatch("pageshow", {});
    h.win.dispatch("beforeunload");
    expect(h.open().length).toBe(0);
    // through every transition there was never more than one live EventSource
    expect(h.made.length).toBe(5);
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
