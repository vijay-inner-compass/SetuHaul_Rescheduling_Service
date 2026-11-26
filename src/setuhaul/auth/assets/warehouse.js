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

// Driver chats and live refresh.
(function () {
  "use strict";
  // The parts that change on their own. Forms are left alone so nothing being typed is lost.
  var LIVE = [".wh .attention", "#board .bays", "#board .scroll", "#messages"];
  var EVERY_MS = 3000;
  var seen = {};
  // What the server last sent, taken before any script changes the page.
  LIVE.forEach(function (sel) {
    var node = document.querySelector(sel);
    if (node) { seen[sel] = node.cloneNode(true); }
  });
  var chosen = null;

  // Show one driver's conversation; `keep` is a remembered scroll position, null means the newest message.
  function open(driver, keep) {
    Array.prototype.forEach.call(document.querySelectorAll("button[data-thread]"), function (b) {
      var on = b.getAttribute("data-driver") === driver;
      b.setAttribute("aria-pressed", String(on));
      var thread = document.getElementById("thread-" + b.getAttribute("data-thread"));
      thread.hidden = !on;
      if (on) {
        var list = thread.querySelector("ol");
        list.scrollTop = keep === null ? list.scrollHeight : keep;
      }
    });
  }
  // The chosen driver if still listed, otherwise the first one.
  function reopen(keep) {
    var all = document.querySelectorAll("button[data-driver]");
    if (!all.length) { return; }
    var known = Array.prototype.some.call(all, function (b) { return b.getAttribute("data-driver") === chosen; });
    if (!known) { chosen = all[0].getAttribute("data-driver"); keep = null; }
    open(chosen, keep);
  }
  // null while the reader is at the newest message, so new ones scroll into view; otherwise leave them where they are.
  function position() {
    var list = document.querySelector(".thread:not([hidden]) ol");
    if (!list || list.scrollHeight - list.scrollTop - list.clientHeight < 40) { return null; }
    return list.scrollTop;
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-driver]");
    if (button) { chosen = button.getAttribute("data-driver"); open(chosen, null); }
  });
  // A thread opened while its tab was hidden has no height yet, so scroll again when the tab appears.
  window.addEventListener("hashchange", function () { reopen(null); });
  reopen(null);

  // What identifies a control across a refresh: its key, or the driver a chat button belongs to.
  function find(root, mark) {
    return mark ? root.querySelector("[" + mark[0] + "='" + CSS.escape(mark[1]) + "']") : null;
  }
  function markOf(node) {
    var name = node && node.getAttribute && (node.hasAttribute("data-key") ? "data-key" : node.hasAttribute("data-driver") ? "data-driver" : "");
    return name ? [name, node.getAttribute(name)] : null;
  }

  // Remember what the user had in a section that is about to be replaced; returns how to put it back.
  function remember(section) {
    var active = section.contains(document.activeElement) ? document.activeElement : null;
    var focus = markOf(active);
    // A control that disappears (a Claim button, say) hands focus to its card.
    var card = active && markOf(active.closest("[data-key^='case:']"));
    var open = Array.prototype.map.call(section.querySelectorAll("details[open][data-key]"), markOf);
    var typed = Array.prototype.filter.call(section.querySelectorAll("textarea[data-key], select[data-key]"), function (f) {
      return f.value !== "";
    }).map(function (f) { return [markOf(f), f.value]; });
    var errors = Array.prototype.map.call(section.querySelectorAll("[data-key^='case:'] > .action-note"), function (e) {
      return [markOf(e.parentNode), e];
    });
    var chats = section.querySelector(".chat-list");
    var listTop = chats ? chats.scrollTop : 0;
    return function (fresh) {
      open.forEach(function (mark) { var d = find(fresh, mark); if (d) { d.open = true; } });
      typed.forEach(function (pair) { var f = find(fresh, pair[0]); if (f) { f.value = pair[1]; } });
      errors.forEach(function (pair) {
        var holder = find(fresh, pair[0]);
        if (holder) { holder.appendChild(pair[1]); } else { window.SH.say(pair[1].textContent); }
      });
      var list = fresh.querySelector(".chat-list");
      if (list) { list.scrollTop = listTop; }
      var target = find(fresh, focus) || find(fresh, card);
      if (target) { target.focus({ preventScroll: true }); }
    };
  }

  // Replacing a box mid-sentence would break the caret and Hindi input composition, so that section waits.
  function typing(section) {
    var active = document.activeElement;
    return !!active && active.tagName === "TEXTAREA" && section.contains(active);
  }

  function apply(page) {
    var keep = position();
    var changed = false;
    LIVE.forEach(function (sel) {
      var fresh = page.querySelector(sel);
      var current = document.querySelector(sel);
      if (!fresh || !current || (seen[sel] && fresh.isEqualNode(seen[sel])) || typing(current)) { return; }
      seen[sel] = fresh.cloneNode(true);
      var node = document.importNode(fresh, true);
      node.hidden = current.hidden;
      var restore = remember(current);
      current.replaceWith(node);
      // The chat shown is chosen first, so a focused control inside it is visible when focus returns.
      if (sel === "#messages") { reopen(keep); }
      restore(node);
      changed = true;
    });
    if (changed) { reopen(keep); }
  }

  var busy = false;
  var timer = window.setInterval(function () {
    if (document.hidden || busy) { return; }
    busy = true;
    fetch(window.location.pathname + window.location.search, { credentials: "same-origin", redirect: "manual" })
      .then(function (response) {
        // Signed out or refused: stop asking and say so.
        if (!response.ok) {
          window.clearInterval(timer);
          window.SH.say("Live updates stopped. Reload the page and sign in again.");
          return null;
        }
        return response.text();
      })
      .then(function (text) {
        if (text) { apply(new DOMParser().parseFromString(text, "text/html")); }
      })
      .catch(function () { /* A dropped request: the next tick tries again. */ })
      .then(function () { busy = false; });
  }, EVERY_MS);
})();
