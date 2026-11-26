// Shared behaviour for every screen: forms and buttons marked with data-api post JSON to the existing APIs, then reload.
// Text is only ever written with textContent; nothing here builds markup from strings.
(function () {
  "use strict";

  // Where a success message and the focused control wait while the page reloads.
  var FLASH = "sh-flash";

  // The words for each reason code a screen can meet. BAD_CREDENTIALS reads the same whether or not the account is locked.
  var MESSAGES = {
    BAD_CREDENTIALS: "The email or password is not correct. Repeated failures lock the password for a while.",
    UNAUTHORIZED: "Your session has ended.",
    UNAUTHENTICATED: "Your session has ended.",
    FORBIDDEN: "Your account is not allowed to do this.",
    CROSS_SITE_REQUEST: "The request was blocked because it did not come from this site's address. Reload the page.",
    DATABASE_UNAVAILABLE: "The service cannot reach its database. Nothing was changed. Try again shortly.",
    INVALID_REQUEST: "Some fields are missing or not valid. Nothing was changed.",
    NOT_FOUND: "That item no longer exists, or it is outside your facilities.",
    ALREADY_CLAIMED: "Someone else has already claimed this case.",
    NOT_OPEN: "This case is no longer open. Reload to see its current state.",
    NOT_CLAIMED: "Claim the case before resolving it.",
    NOT_YOUR_CASE: "Only the person who claimed this case can resolve it.",
    ALREADY_FINISHED: "This case is already closed.",
    NOT_RESOLVED: "This case cannot be reopened: it is not resolved, or a newer case for the same problem is active.",
    REOPEN_WINDOW_PASSED: "It is too late to reopen this case. A new report opens a new case.",
    INVALID_RESOLUTION: "Choose an outcome.",
    ALREADY_INVITED: "That email address has already been invited.",
    FACILITY_REQUIRED: "Choose at least one facility for a warehouse authority.",
    FACILITY_NOT_FOUND: "That facility was not found.",
    INVALID_EMAIL: "Enter a valid email address.",
    INVALID_NAME: "Enter a name.",
    INVALID_PHONE: "Enter a valid phone number, for example +91 90000 12345.",
    DUPLICATE_PHONE: "Another driver already has that phone number.",
    DUPLICATE_LICENCE: "Another driver already has that licence number.",
    DUPLICATE_CONTRACT: "A project with that contract reference already exists.",
    INVALID_LICENCE_EXPIRY: "Enter a valid licence expiry date.",
    LICENCE_EXPIRED: "That licence has expired.",
    VEHICLE_CARRIER_MISMATCH: "That vehicle belongs to a different carrier.",
    VEHICLE_INACTIVE: "That vehicle is not active.",
    VEHICLE_NOT_FOUND: "That vehicle was not found.",
    DRIVER_NOT_FOUND: "That driver was not found.",
    DRIVER_NOT_ELIGIBLE: "That driver is not eligible: check approval and licence.",
    ALREADY_ALLOCATED: "That driver is already allocated to this project.",
    NOT_ALLOCATED: "That driver is not allocated to this project.",
    PROJECT_NOT_FOUND: "That project was not found.",
    PROJECT_CLOSED: "That project is closed.",
    PROJECT_ENDED: "That project has ended.",
    CANNOT_DISABLE_SELF: "You cannot disable your own account.",
    LAST_ADMIN: "The last administrator cannot be disabled.",
    NOT_PENDING: "This item is no longer waiting for a decision.",
    NOT_BOUND: "That driver has no linked Telegram account.",
    NOT_REVOKED: "That driver's Telegram access is not revoked."
  };

  // Codes that mean "sign in again".
  var SIGN_IN = { UNAUTHORIZED: true, UNAUTHENTICATED: true };

  // Show a message on the page's status line.
  function say(text, good) {
    var line = document.getElementById("status");
    if (line) {
      line.textContent = text;
      line.classList.toggle("ok", !!good);
    }
  }

  // The words for a refused call. A code with no sentence yet is still named, so it can be reported.
  function reason(status, data) {
    var code = data && typeof data.reason_code === "string" ? data.reason_code : status === 422 ? "INVALID_REQUEST" : "";
    // A sentence from the server says the most (which dock rule failed, say); then ours; then at least the code.
    var text = data && typeof data.message === "string" ? data.message :
      Object.prototype.hasOwnProperty.call(MESSAGES, code) ? MESSAGES[code] :
      "The request was refused (" + (code || status) + "). Nothing was changed.";
    // A claim lost to someone else names them (a web identity is "web:" plus their email).
    if (code === "ALREADY_CLAIMED" && data.claimed_by) {
      text = "Already claimed by " + String(data.claimed_by).replace(/^web:/, "") + ".";
    }
    return { text: text, signIn: !!SIGN_IN[code] };
  }

  // Put the refusal next to what was pressed. The sign-in page keeps its own status line.
  function explain(source, why) {
    if (document.body.classList.contains("login")) {
      say(why.text);
      return;
    }
    say("");
    var holder = source.closest(".case") || (source.tagName === "FORM" ? source : source.parentNode);
    if (!source.isConnected) {
      // The live refresh replaced what was pressed: use the new card, or the status line if it has gone.
      var old = source.closest("[data-key^='case:']");
      holder = old && document.querySelector("[data-key='" + CSS.escape(old.getAttribute("data-key")) + "']");
      if (!holder) {
        say(why.text);
        return;
      }
    }
    // A form that has its own place for the answer gets it there.
    var box = holder.querySelector("[data-form-result]") || holder.querySelector(":scope > .action-note");
    if (!box) {
      box = document.createElement("p");
      box.className = "action-note";
      box.setAttribute("role", "alert");
      holder.appendChild(box);
    }
    box.textContent = why.text + " ";
    if (why.signIn) {
      var link = document.createElement("a");
      link.setAttribute("href", "/");
      link.textContent = "Sign in again";
      box.appendChild(link);
    }
    box.scrollIntoView({ block: "nearest" });
  }

  // POST JSON; on success go to `next` (or reload), otherwise say why beside `source`. While a request is out the
  // pressed button is off, so a second press sends nothing.
  function send(source, button, body) {
    if (source.getAttribute("data-busy")) {
      return;
    }
    var label = button ? button.textContent : "";
    var next = source.getAttribute("data-next");
    function release() {
      source.removeAttribute("data-busy");
      if (button) {
        button.disabled = false;
        button.textContent = label;
      }
    }
    source.setAttribute("data-busy", "true");
    if (button) {
      button.disabled = true;
      button.textContent = "Working...";
    }
    fetch(source.getAttribute("data-api"), {
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
            var card = source.closest("[data-key^='case:']");
            window.sessionStorage.setItem(FLASH, JSON.stringify({
              text: source.getAttribute("data-done") || "Done.",
              focus: card ? card.getAttribute("data-key") : ""
            }));
            window.location.reload();
          }
          return;
        }
        release();
        explain(source, reason(response.status, data));
      });
    }).catch(function () {
      release();
      explain(source, { text: "The service could not be reached. Nothing is known to have changed.", signIn: false });
    });
  }

  // After the reload: say it worked, and go back to the case that was acted on.
  function flash() {
    var raw = window.sessionStorage.getItem(FLASH);
    if (!raw) {
      return;
    }
    window.sessionStorage.removeItem(FLASH);
    var kept = JSON.parse(raw);
    say(kept.text, true);
    // Waits for the tab script, which unhides the section the card is in.
    window.setTimeout(function () {
      var card = kept.focus && document.querySelector("[data-key='" + CSS.escape(kept.focus) + "']");
      if (card) {
        var fold = card.closest("details");
        if (fold) {
          fold.open = true;
        }
        card.focus();
        // The status line may be out of view, so the card says it too.
        var done = document.createElement("p");
        done.className = "action-note ok";
        done.textContent = kept.text;
        card.appendChild(done);
      }
    }, 0);
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
        submit(form);
      });
    });
    // Forms the live refresh adds have no listener of their own, so they are caught here.
    document.addEventListener("submit", function (event) {
      var form = event.target.closest("form[data-api]");
      if (form && !event.defaultPrevented) {
        event.preventDefault();
        submit(form);
      }
    });
    // One listener on the document, not one per button: the live refresh replaces sections, and their new buttons must work too.
    document.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-api]");
      if (!button) {
        return;
      }
      var ask = button.getAttribute("data-confirm");
      if (ask && !window.confirm(ask)) {
        return;
      }
      var raw = button.getAttribute("data-body");
      send(button, button, raw ? JSON.parse(raw) : {});
    });
    flash();
  }

  // A required box holding only spaces counts as empty; then ask, if the form asks, and send.
  function submit(form) {
    if (form.getAttribute("data-busy")) {
      return;
    }
    var blank = Array.prototype.filter.call(form.querySelectorAll("textarea[required]"), function (box) {
      return !box.value.trim();
    })[0];
    if (blank) {
      explain(form, { text: "Write the message first.", signIn: false });
      blank.focus();
      return;
    }
    var ask = form.getAttribute("data-confirm");
    if (ask && !window.confirm(ask)) {
      return;
    }
    send(form, form.querySelector("button[type=submit]"), collect(form));
  }

  window.SH = { init: init, say: say };
  init();
})();
