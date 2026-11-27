// The set-password screen: takes the link's token from after "#" and checks that the two passwords match.
(function () {
  "use strict";

  var token = document.getElementById("token");
  var password = document.getElementById("password");
  var repeat = document.getElementById("repeat");
  var found = /^#token=([A-Za-z0-9_-]+)$/.exec(window.location.hash);
  if (found) {
    token.value = found[1];
    // Out of the address bar and the history once it has been read.
    window.history.replaceState(null, "", window.location.pathname);
  } else {
    // A reload lands here too, because the token is no longer in the address.
    document.getElementById("status").textContent = "Open the link from your email to set a password.";
  }

  function compare() {
    repeat.setCustomValidity(repeat.value === password.value ? "" : "The passwords do not match.");
  }
  password.addEventListener("input", compare);
  repeat.addEventListener("input", compare);
})();
