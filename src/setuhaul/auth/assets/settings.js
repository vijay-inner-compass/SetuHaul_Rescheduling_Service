// Configuration tab: send only what the admin changed, and show check results as text.
(function () {
  "use strict";

  // The fields that differ from what the page was drawn with. A password box counts when something was typed.
  function changed(form) {
    var body = {};
    Array.prototype.forEach.call(form.elements, function (field) {
      if (!field.name || field.disabled) {
        return;
      }
      if (field.tagName === "SELECT") {
        var drawn = Array.prototype.filter.call(field.options, function (o) { return o.defaultSelected; })[0] || field.options[0];
        if (field.value !== drawn.value) {
          body[field.name] = field.value;
        }
      } else if (field.type === "password") {
        if (field.value !== "") {
          body[field.name] = field.value;
        }
      } else if (field.value !== field.defaultValue) {
        body[field.name] = field.value;
      }
    });
    return body;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    });
  }

  function refusal(result) {
    return window.SH.reason(result.status, result.data).text;
  }

  function save(form, body) {
    var section = form.getAttribute("data-settings");
    if (!Object.keys(body).length) {
      window.SH.say("Nothing to save.");
      return;
    }
    // Turning tracing on sends data off the server: ask first.
    if (section === "langsmith" && body.enabled === "on") {
      if (!window.confirm("Conversation data will be sent to LangSmith from this server. Continue?")) {
        return;
      }
      body.confirm = "SEND_TO_LANGSMITH";
    }
    if (!window.SH.keep(form)) {
      return;
    }
    window.SH.say("Working...");
    post("/v1/admin/settings/" + section, body).then(function (result) {
      if (result.ok) {
        window.location.reload();
      } else {
        window.SH.say(refusal(result));
      }
    }).catch(function () { window.SH.say("Network error"); });
  }

  Array.prototype.forEach.call(document.querySelectorAll("form[data-settings]"), function (form) {
    var out = form.parentNode.querySelector("[data-result]");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      save(form, changed(form));
    });
    Array.prototype.forEach.call(form.querySelectorAll("button[data-generate]"), function (button) {
      button.addEventListener("click", function () {
        var body = changed(form);
        delete body.webhook_secret;
        body.generate_secret = "yes";
        save(form, body);
      });
    });
    Array.prototype.forEach.call(form.querySelectorAll("button[data-check]"), function (button) {
      button.addEventListener("click", function () {
        var url = button.getAttribute("data-check");
        var typed = changed(form);
        var body = {};
        // Each check takes only the fields it knows; registering takes none.
        if (url.indexOf("/model/test") !== -1 || url.indexOf("/email/test") !== -1) {
          body = typed;
        } else if (url.indexOf("/telegram/test") !== -1 && typed.bot_token) {
          body = { bot_token: typed.bot_token };
        }
        button.disabled = true;
        out.textContent = "Checking...";
        post(url, body).then(function (result) {
          out.textContent = result.ok ? (result.data.ok ? "Worked: " : "Failed: ") + result.data.message : refusal(result);
        }).catch(function () { out.textContent = "Network error"; })
          .then(function () { button.disabled = false; });
      });
    });
  });
})();
