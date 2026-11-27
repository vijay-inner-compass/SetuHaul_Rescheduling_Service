// Tabs: one section at a time. The address keeps the choice, so a reload after an action stays put.
(function () {
  "use strict";
  var tabs = document.querySelectorAll(".tabs a[data-tab]");
  // A page without tabs is left alone.
  if (!tabs.length) {
    return;
  }
  function show() {
    // Looked up each time: the live refresh replaces whole sections.
    var panels = document.querySelectorAll("[data-panel]");
    var wanted = window.location.hash.slice(1);
    // The messages moved into a panel beside the board; old links to them still open it.
    if (wanted === "messages") { wanted = "board"; }
    var known = Array.prototype.some.call(panels, function (p) { return p.getAttribute("data-panel") === wanted; });
    // No choice, or an unknown one: the first tab.
    var key = known ? wanted : tabs[0].getAttribute("data-tab");
    Array.prototype.forEach.call(panels, function (p) { p.hidden = p.getAttribute("data-panel") !== key; });
    Array.prototype.forEach.call(tabs, function (a) {
      a.setAttribute("aria-current", a.getAttribute("data-tab") === key ? "page" : "false");
    });
  }
  window.addEventListener("hashchange", show);
  show();
})();
