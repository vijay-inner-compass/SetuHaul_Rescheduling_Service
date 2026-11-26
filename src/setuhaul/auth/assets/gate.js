// Gate screen: check the pass, then offer Check in for exactly what was checked. Text is only written with textContent.
(function () {
  "use strict";
  var form = document.getElementById("gate-form");
  var result = document.getElementById("gate-result");
  var checkIn = document.getElementById("check-in");
  if (!form || !result || !checkIn) {
    return;
  }
  // The values that passed the check, with the key for this truck.
  var checked = null;

  function post(path, body, done) {
    result.textContent = "Working...";
    fetch(path, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (data) {
        if (!response.ok) {
          result.textContent = (data && (data.message || data.reason_code)) || "Request failed (" + response.status + ")";
          return;
        }
        done(data);
      });
    }).catch(function () {
      result.textContent = "Network error";
    });
  }

  function forget() {
    checked = null;
    checkIn.hidden = true;
  }

  // Any change means what is shown no longer describes what is typed.
  form.addEventListener("input", forget);

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    forget();
    var body = {
      code: form.elements.code.value, vehicle_registration: form.elements.vehicle_registration.value,
      licence_number: form.elements.licence_number.value, request_key: window.crypto.randomUUID()
    };
    post("/v1/gate/validate", body, function (data) {
      checked = body;
      result.textContent = "Checks passed: " + data.driver_name + ", " + data.vehicle_registration + ", order " +
        data.order_reference + ", dock " + (data.dock_code || "not set") + ".";
      checkIn.hidden = false;
    });
  });

  checkIn.addEventListener("click", function () {
    if (!checked) {
      return;
    }
    post("/v1/gate/check-in", checked, function () {
      // The code does not stay on the screen.
      form.reset();
      forget();
      window.location.reload();
    });
  });
})();
