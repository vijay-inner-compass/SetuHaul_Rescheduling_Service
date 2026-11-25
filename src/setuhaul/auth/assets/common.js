// Shared behaviour for every screen: forms and buttons marked with data-api post JSON to the existing APIs, then reload.
// Text is only ever written with textContent; nothing here builds markup from strings.
(function () {
  "use strict";

  // Show a message on the page's status line.
  function say(text) {
    var line = document.getElementById("status");
    if (line) {
      line.textContent = text;
    }
  }

  // The words for a refused call: the API's reason code when it sent one.
  function reason(status, data) {
    // The one refusal people meet often gets plain words. It reads the same whether or not the account is locked.
    if (data && data.reason_code === "BAD_CREDENTIALS") {
      return "The email or password is not correct. Repeated failures lock the password for a while.";
    }
    if (data && typeof data.reason_code === "string") {
      return data.reason_code;
    }
    if (status === 422) {
      return "INVALID_REQUEST";
    }
    return "Request failed (" + status + ")";
  }

  // POST JSON; on success go to `next` (or reload), otherwise say why it failed.
  function send(url, body, next) {
    say("Working...");
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (data) {
        if (response.ok) {
          if (next) {
            window.location.assign(next);
          } else {
            window.location.reload();
          }
          return;
        }
        say(reason(response.status, data));
      });
    }).catch(function () {
      say("Network error");
    });
  }

  // Read a form into a JSON object: multi-selects become arrays, empty fields are left out.
  function collect(form) {
    var body = {};
    Array.prototype.forEach.call(form.elements, function (field) {
      if (!field.name || field.disabled || field.type === "submit" || field.type === "button") {
        return;
      }
      if (field.multiple) {
        body[field.name] = Array.prototype.filter.call(field.options, function (o) { return o.selected; })
          .map(function (o) { return o.value; });
      } else if (field.value !== "") {
        body[field.name] = field.value;
      }
    });
    return body;
  }

  // A "Show" button beside a password field switches it between hidden and readable.
  function reveal(button) {
    var field = document.getElementById(button.getAttribute("data-reveal"));
    var show = field.type === "password";
    field.type = show ? "text" : "password";
    button.textContent = show ? "Hide" : "Show";
    button.setAttribute("aria-pressed", String(show));
  }

  // Wire every data-api form and button on the page.
  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("button[data-reveal]"), function (button) {
      button.addEventListener("click", function () { reveal(button); });
    });
    Array.prototype.forEach.call(document.querySelectorAll("form[data-api]"), function (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        send(form.getAttribute("data-api"), collect(form), form.getAttribute("data-next"));
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll("button[data-api]"), function (button) {
      button.addEventListener("click", function () {
        var ask = button.getAttribute("data-confirm");
        if (ask && !window.confirm(ask)) {
          return;
        }
        var raw = button.getAttribute("data-body");
        send(button.getAttribute("data-api"), raw ? JSON.parse(raw) : {}, button.getAttribute("data-next"));
      });
    });
  }

  window.SH = { init: init, say: say };
  init();
})();
