// papi live-feed ordering: pure functions shared by app.js (no DOM access here).
// Keys mirror the server's ORDER BY (papi.web.queries._order):
//   time_desc   published DESC, KAP index DESC, id DESC   (default)
//   time_asc    published ASC,  KAP index ASC,  id ASC
//   materiality materiality DESC, then time_desc
// A missing publish time sorts last in every order (SQL "NULLS LAST"); a missing materiality is -1.
(function (root) {
  "use strict";

  function cmpTime(a, b) {  // ascending by (published, ref, id); "" handled by caller
    if (a.published !== b.published) return a.published < b.published ? -1 : 1;
    if (a.ref !== b.ref) return a.ref - b.ref;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  }

  // < 0 when `a` belongs above `b` in the rendered list.
  function cmp(a, b, sort) {
    if (sort === "materiality" && a.mat !== b.mat) return b.mat - a.mat;
    var ea = a.published === "", eb = b.published === "";
    if (ea !== eb) return ea ? 1 : -1;
    var t = cmpTime(a, b);
    return sort === "time_asc" ? t : -t;
  }

  // Where does `key` go among the rendered `list` (already in server order)?
  //   {index: i}    insert before list[i]
  //   {index: -1}   append after the last rendered item (the list is complete)
  //   {beyond: true} it sorts after the last rendered item but more items exist below
  //                 (not rendered yet) → don't insert; the load-more link will pick it up
  // `isNew` is true only when it is newer than every rendered item (only those animate as "yeni").
  function decide(key, list, sort, hasMore) {
    var newest = "";
    for (var j = 0; j < list.length; j++) if (list[j].published > newest) newest = list[j].published;
    var isNew = key.published !== "" && key.published > newest;
    for (var i = 0; i < list.length; i++) {
      if (cmp(key, list[i], sort) < 0) return { index: i, isNew: isNew };
    }
    if (list.length && hasMore) return { beyond: true, isNew: isNew };
    return { index: -1, isNew: isNew };
  }

  function keyOf(el) {
    var d = el.dataset;
    return { id: d.id || "", published: d.published || "", ref: +d.ref || 0, mat: d.mat === undefined ? -1 : +d.mat };
  }

  root.papiFeed = { cmp: cmp, decide: decide, keyOf: keyOf };
})(typeof window !== "undefined" ? window : globalThis);
