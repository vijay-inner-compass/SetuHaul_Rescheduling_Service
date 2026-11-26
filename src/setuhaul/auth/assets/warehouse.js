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
  var LIVE = [".wh .attention", "#board .asof", "#board .bays", "#board .scroll", "#board .waiting", "#messages"];
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
      var across = current.scrollLeft;
      current.replaceWith(node);
      // The hour the reader scrolled the board to stays in view.
      node.scrollLeft = across;
      // The chat shown is chosen first, so a focused control inside it is visible when focus returns.
      if (sel === "#messages") { reopen(keep); }
      restore(node);
      changed = true;
    });
    if (changed) { reopen(keep); }
  }

  // Say how fresh the live parts are. The state is written only when it changes, so it is announced once.
  var STALE_MS = 6000;
  var line = document.querySelector("[data-live]");
  function freshness(state, stale) {
    if (!line) { return; }
    var node = line.querySelector("[data-live-state]");
    if (node.textContent !== state) { node.textContent = state; }
    line.classList.toggle("stale", stale);
  }
  function updated() {
    failures = 0;
    freshness("Live updates on.", false);
    if (line) { line.querySelector("[data-live-time]").textContent = "Last updated " + new Date().toLocaleTimeString() + "."; }
  }
  function paused() {
    freshness("Updates paused: the service is not answering. Retrying. What you see may be out of date.", true);
  }

  var busy = false;
  var asked = 0;
  var failures = 0;
  updated();
  var timer = window.setInterval(function () {
    if (document.hidden) { return; }
    if (busy) {
      // A request that hangs is as stale as one that fails.
      if (Date.now() - asked > STALE_MS) { paused(); }
      return;
    }
    busy = true;
    asked = Date.now();
    fetch(window.location.pathname + window.location.search,
      { credentials: "same-origin", redirect: "manual", signal: AbortSignal.timeout(2 * STALE_MS) })
      .then(function (response) {
        // Signed out or refused: stop asking and say so.
        if (response.type === "opaqueredirect" || response.status === 401 || response.status === 403) {
          window.clearInterval(timer);
          freshness("Live updates stopped. Reload the page and sign in again.", true);
          return null;
        }
        // Any other failure (a busy database, say) is tried again.
        if (!response.ok) { throw new Error("unavailable"); }
        return response.text();
      })
      .then(function (text) {
        if (text) {
          apply(new DOMParser().parseFromString(text, "text/html"));
          updated();
        }
      })
      .catch(function () {
        failures += 1;
        if (failures >= 2) { paused(); }
      })
      .then(function () { busy = false; });
  }, EVERY_MS);
})();

// Shipments tab: offer only what belongs to the chosen facility and carrier, and report what opening availability did.
(function () {
  "use strict";

  // Disable the options whose data attribute does not match; move off a disabled choice.
  function limit(select, key, wanted) {
    if (!select || !wanted) {
      return;
    }
    Array.prototype.forEach.call(select.options, function (option) {
      var off = option.getAttribute("data-" + key) !== wanted;
      option.disabled = off;
      option.hidden = off;
      if (off) {
        option.selected = false;
      }
    });
    if (!select.multiple && (select.selectedIndex < 0 || select.options[select.selectedIndex].disabled)) {
      var first = Array.prototype.filter.call(select.options, function (o) { return !o.disabled; })[0];
      select.value = first ? first.value : "";
    }
  }

  Array.prototype.forEach.call(document.querySelectorAll("form[data-limit]"), function (form) {
    var facility = form.elements.facility_id;
    var driver = form.elements.driver_id;
    var sync = function () {
      if (facility) {
        limit(form.elements.dock_ids, "facility", facility.value);
        limit(form.elements.slot_id, "facility", facility.value);
      }
      if (driver && driver.selectedIndex >= 0) {
        limit(form.elements.vehicle_id, "carrier", driver.options[driver.selectedIndex].getAttribute("data-carrier"));
      }
    };
    form.addEventListener("change", sync);
    sync();
  });

  // Opening availability reports days it left alone, so the answer is kept across the reload that refreshes the slot list.
  var form = document.getElementById("availability-form");
  var line = document.getElementById("availability-result");
  if (!form || !line) {
    return;
  }
  line.textContent = window.sessionStorage.getItem("availability") || "";
  window.sessionStorage.removeItem("availability");
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var body = {};
    Array.prototype.forEach.call(form.elements, function (field) {
      if (field.multiple) {
        body[field.name] = Array.prototype.filter.call(field.options, function (o) { return o.selected; })
          .map(function (o) { return o.value; });
      } else if (field.name && field.value !== "") {
        body[field.name] = field.value;
      }
    });
    line.textContent = "Working...";
    fetch("/v1/warehouse/availability", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (data) {
        if (!response.ok) {
          line.textContent = (data && (data.message || data.reason_code)) || "INVALID_REQUEST";
          return;
        }
        var text = data.slots_created + " slots added.";
        if (data.days_unchanged.length) {
          text += " Labour already recorded, left as it was: " + data.days_unchanged.join(", ") + ".";
        }
        if (data.slots_skipped) {
          text += " " + data.slots_skipped + " slots not added: no labour is recorded for them.";
        }
        window.sessionStorage.setItem("availability", text);
        window.location.reload();
      });
    }).catch(function () {
      line.textContent = "Network error";
    });
  });
})();
