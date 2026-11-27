// Blocking a dock: first show what it would do to the bookings, then confirm exactly that.
// Text is only ever written with textContent; nothing here builds markup from strings.
(function () {
  "use strict";
  var form = document.getElementById("block-form");
  var box = document.getElementById("block-warning");
  var line = document.getElementById("block-result");
  if (!form || !box || !line) {
    return;
  }
  // What happened survives the reload that refreshes the board.
  line.textContent = window.sessionStorage.getItem("exceptions") || "";
  window.sessionStorage.removeItem("exceptions");

  function post(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (data) {
        return { ok: response.ok, data: data || {} };
      });
    });
  }
  function values() {
    var body = {};
    Array.prototype.forEach.call(form.elements, function (field) {
      if (field.name && field.value !== "") { body[field.name] = field.value; }
    });
    return body;
  }
  function add(parent, tag, text) {
    var node = document.createElement(tag);
    node.textContent = text;
    parent.appendChild(node);
    return node;
  }
  function when(stamp) {
    return stamp.slice(8, 10) + "/" + stamp.slice(5, 7) + " " + stamp.slice(11, 16);
  }
  function outcome(item) {
    if (item.action === "MOVE") { return "Move to " + item.new_dock_code + " " + when(item.new_start); }
    if (item.action === "FREE") { return "None fits: slot freed, needs a new slot"; }
    return item.why === "UNLOADING" ? "Left alone: unloading" : "Left alone: truck on site";
  }

  function show(items, sent, note) {
    box.textContent = "";
    if (note) { add(box, "p", note); }
    add(box, "p", items.length ? items.length + " booking(s) affected:" : "No bookings are affected.");
    if (items.length) {
      var list = add(box, "ul", "");
      items.forEach(function (item) {
        add(list, "li", item.order_reference + ", " + item.driver_name + ": " + item.dock_code + " " + when(item.start) + ". " +
          outcome(item));
      });
    }
    var acting = items.filter(function (item) { return item.action !== "SKIP"; });
    var go = add(box, "button", acting.length ? "Block and move" : "Block");
    go.type = "button";
    go.addEventListener("click", function () {
      go.disabled = true;
      var body = JSON.parse(JSON.stringify(sent));
      body.expected = acting.map(function (item) {
        return { appointment_id: item.appointment_id, new_slot_id: item.new_slot_id };
      });
      post("/v1/warehouse/dock-blocks", body).then(function (answer) {
        if (answer.ok) {
          var missed = answer.data.unreachable || [];
          window.sessionStorage.setItem("exceptions", "Dock blocked." +
            (missed.length ? " Could not reach the driver of: " + missed.join(", ") + ". Please phone them." : ""));
          window.location.reload();
        } else if (answer.data.reason_code === "PLAN_CHANGED") {
          show(answer.data.items, sent, answer.data.message);
        } else {
          go.disabled = false;
          line.textContent = answer.data.message || answer.data.reason_code || "INVALID_REQUEST";
        }
      }).catch(function () {
        go.disabled = false;
        line.textContent = "Network error";
      });
    });
  }

  // Any edit makes the warning out of date.
  form.addEventListener("input", function () { box.textContent = ""; });
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var sent = values();
    box.textContent = "";
    line.textContent = "Checking...";
    post("/v1/warehouse/dock-blocks/preview", sent).then(function (answer) {
      line.textContent = answer.ok ? "" : answer.data.message || answer.data.reason_code || "INVALID_REQUEST";
      if (answer.ok) { show(answer.data.items, sent, ""); }
    }).catch(function () {
      line.textContent = "Network error";
    });
  });
})();

// Deleting a driver: show what it would cancel, ask for the name, then confirm exactly that list.
(function () {
  "use strict";
  var box = document.getElementById("driver-removal");
  if (!box) {
    return;
  }
  function add(parent, tag, text) {
    var node = document.createElement(tag);
    node.textContent = text;
    parent.appendChild(node);
    return node;
  }
  function read(response) {
    return response.json().catch(function () { return null; }).then(function (data) {
      return { ok: response.ok, data: data || {} };
    });
  }
  function why(data) {
    return data.message || data.reason_code || "INVALID_REQUEST";
  }

  function show(driver, note) {
    var base = "/v1/warehouse/drivers/" + encodeURIComponent(driver);
    box.textContent = "Checking...";
    fetch(base + "/deletion", { credentials: "same-origin" }).then(read).then(function (answer) {
      box.textContent = "";
      if (!answer.ok) {
        add(box, "p", why(answer.data));
        return;
      }
      var plan = answer.data;
      add(box, "h3", "Delete " + plan.driver_name + " (" + plan.driver_id + ")?");
      if (note) { add(box, "p", note); }
      add(box, "p", plan.shipments.length
        ? "This cancels " + plan.shipments.length + " unfinished shipment(s) and frees their dock slots:"
        : "This driver has no unfinished shipments.");
      if (plan.shipments.length) {
        var list = add(box, "ul", "");
        plan.shipments.forEach(function (s) {
          var where = s.dock_code ? s.dock_code + " " + s.start.slice(8, 10) + "/" + s.start.slice(5, 7) + " " + s.start.slice(11, 16)
            : "no slot booked";
          add(list, "li", s.order_reference + ": " + where + (s.blocked_by ? " (truck on site: finish this visit first)" : ""));
        });
      }
      var label = add(box, "label", "Type the driver's name to confirm ");
      var name = add(label, "input", "");
      name.autocomplete = "off";
      var go = add(box, "button", "Delete driver");
      go.type = "button";
      var stop = add(box, "button", "Cancel");
      stop.type = "button";
      stop.addEventListener("click", function () { box.textContent = ""; });
      var line = add(box, "p", "");
      line.setAttribute("role", "alert");
      go.addEventListener("click", function () {
        go.disabled = true;
        fetch(base + "/delete", {
          method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm_name: name.value, expected: plan.shipments.map(function (s) { return s.shipment_id; }) })
        }).then(read).then(function (done) {
          if (done.ok) {
            window.location.reload();
          } else if (done.data.reason_code === "PLAN_CHANGED") {
            show(driver, why(done.data));
          } else {
            go.disabled = false;
            line.textContent = why(done.data);
          }
        }).catch(function () {
          go.disabled = false;
          line.textContent = "Network error";
        });
      });
      name.focus();
    }).catch(function () {
      box.textContent = "Network error";
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-delete-driver]");
    if (button) { show(button.getAttribute("data-delete-driver"), ""); }
  });
})();
