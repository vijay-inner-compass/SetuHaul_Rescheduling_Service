// Warehouse screen: switching facility reloads the board; picking a vehicle selects its carrier.
(function () {
  "use strict";

  // Changing the facility shows that facility's board at once.
  var facility = document.querySelector("#board select[name=facility]");
  if (facility && facility.form) {
    facility.addEventListener("change", function () {
      facility.form.submit();
    });
  }

  // The add-driver form: a vehicle belongs to one carrier, so choosing it sets the carrier too.
  var vehicle = document.getElementById("vehicle");
  var carrier = document.getElementById("carrier");
  if (vehicle && carrier) {
    var sync = function () {
      var chosen = vehicle.options[vehicle.selectedIndex];
      if (chosen && chosen.getAttribute("data-carrier")) {
        carrier.value = chosen.getAttribute("data-carrier");
      }
    };
    vehicle.addEventListener("change", sync);
    sync();
  }
})();

// Tabs: one section at a time. The address keeps the choice, so a reload after an action stays put.
(function () {
  "use strict";
  var tabs = document.querySelectorAll(".tabs a[data-tab]");
  var panels = document.querySelectorAll("[data-panel]");
  if (!tabs.length) {
    return;
  }
  function show() {
    var wanted = window.location.hash.slice(1);
    var known = Array.prototype.some.call(panels, function (p) { return p.getAttribute("data-panel") === wanted; });
    var key = known ? wanted : "board";
    Array.prototype.forEach.call(panels, function (p) { p.hidden = p.getAttribute("data-panel") !== key; });
    Array.prototype.forEach.call(tabs, function (a) {
      a.setAttribute("aria-current", a.getAttribute("data-tab") === key ? "page" : "false");
    });
  }
  window.addEventListener("hashchange", show);
  show();
})();

// Driver chats: pick a name, read that conversation, newest message in view.
(function () {
  "use strict";
  var buttons = document.querySelectorAll("button[data-thread]");
  function open(number) {
    Array.prototype.forEach.call(buttons, function (b) {
      var on = b.getAttribute("data-thread") === number;
      b.setAttribute("aria-pressed", String(on));
      var thread = document.getElementById("thread-" + b.getAttribute("data-thread"));
      thread.hidden = !on;
      if (on) {
        var list = thread.querySelector("ol");
        list.scrollTop = list.scrollHeight;
      }
    });
  }
  Array.prototype.forEach.call(buttons, function (b) {
    b.addEventListener("click", function () { open(b.getAttribute("data-thread")); });
  });
  if (buttons.length) {
    open("0");
    // A thread opened while its tab was hidden has no height yet, so scroll again when the tab appears.
    window.addEventListener("hashchange", function () {
      var current = document.querySelector("button[data-thread][aria-pressed=true]");
      if (current) { open(current.getAttribute("data-thread")); }
    });
  }
})();
