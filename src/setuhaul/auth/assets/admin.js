// Admin screen: the common script wires the forms and buttons; here the audit filters get a way back to the full log.
(function () {
  "use strict";

  // A "Clear" link next to the filter form, built with DOM calls only.
  var form = document.querySelector("#audit form.filters");
  if (form) {
    var clear = document.createElement("a");
    clear.setAttribute("href", "/admin");
    clear.textContent = "Clear filters";
    form.appendChild(clear);
  }
})();
