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
          result.textContent = window.SH.reason(response.status, data).text;
          return;
        }
        done(data);
      });
    }).catch(function () {
      result.textContent = "The service could not be reached. Nothing is known to have changed.";
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

  // Pass scanning (F17): extract DAP-... token from wedge/camera scans and advance focus to vehicle number.
  var codeInput = form.elements.code;
  var scanBtn = document.getElementById("scan-pass");
  var scanVideo = document.getElementById("scan-video");
  function normaliseScannedCode(raw) {
    var m = String(raw || "").toUpperCase().match(/DAP-[A-Z0-9-]{6,}/);
    return m ? m[0] : String(raw || "").trim();
  }
  if (codeInput) {
    codeInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && codeInput.value.trim() && !form.elements.vehicle_registration.value.trim()) {
        event.preventDefault();
        codeInput.value = normaliseScannedCode(codeInput.value);
        form.elements.vehicle_registration.focus();
      }
    });
  }
  if (scanBtn && scanVideo && "BarcodeDetector" in window && navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    scanBtn.hidden = false;
    var stream = null;
    function stopScan() {
      if (stream) {
        stream.getTracks().forEach(function (tr) { tr.stop(); });
        stream = null;
      }
      scanVideo.hidden = true;
      scanBtn.textContent = "Scan pass";
    }
    scanBtn.addEventListener("click", function () {
      if (stream) { stopScan(); return; }
      var detector = new window.BarcodeDetector({ formats: ["qr_code", "code_128", "code_39"] });
      navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } }).then(function (s) {
        stream = s;
        scanVideo.srcObject = s;
        scanVideo.hidden = false;
        scanBtn.textContent = "Stop scan";
        return scanVideo.play();
      }).then(function () {
        function tick() {
          if (!stream) { return; }
          detector.detect(scanVideo).then(function (codes) {
            if (codes && codes.length) {
              codeInput.value = normaliseScannedCode(codes[0].rawValue);
              forget();
              stopScan();
              form.elements.vehicle_registration.focus();
              return;
            }
            window.requestAnimationFrame(tick);
          }).catch(function () { stopScan(); });
        }
        window.requestAnimationFrame(tick);
      }).catch(function () {
        result.textContent = "Camera unavailable. Type or use a handheld scanner.";
        stopScan();
      });
    });
  }
})();
