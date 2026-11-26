// Model tab: read the LangSmith summary only when asked, and show it as text.
(function () {
  "use strict";
  var button = document.getElementById("langsmith-load");
  var out = document.getElementById("langsmith-result");
  if (!button || !out) {
    return;
  }

  function line(text) {
    var p = document.createElement("p");
    p.textContent = text;
    out.appendChild(p);
  }
  function seconds(ms) {
    return ms === null ? "-" : (ms / 1000).toFixed(1) + " s";
  }
  function show(data) {
    out.textContent = "";
    if (data.state === "off") {
      line("LangSmith is " + data.message);
    } else if (data.state !== "ok") {
      line(data.message);
    } else {
      line("Last 24 hours: " + data.runs + (data.capped ? "+ (latest " + data.runs + " only)" : "") +
           " conversation runs, " + data.failed + " failed");
      line("Run time: typical " + seconds(data.p50_ms) + ", 95th " + seconds(data.p95_ms));
      line("Latest run: " + (data.latest || "none"));
    }
  }

  button.addEventListener("click", function () {
    button.disabled = true;
    out.textContent = "Loading...";
    fetch("/v1/admin/model/langsmith", { credentials: "same-origin", redirect: "manual" })
      .then(function (response) {
        if (!response.ok) { throw new Error("refused"); }
        return response.json();
      })
      .then(show)
      .catch(function () { out.textContent = "Could not load. Sign in again."; })
      .then(function () { button.disabled = false; });
  });
})();
