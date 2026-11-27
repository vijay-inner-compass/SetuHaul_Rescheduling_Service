// Admin screen: the common script wires the forms and buttons; here the audit filters get a way back to the full log.
(function () {
  "use strict";

  // A "Clear" link next to the filter form, built with DOM calls only.
  var form = document.querySelector("#audit form.filters");
  if (form) {
    var clear = document.createElement("a");
    clear.setAttribute("href", "/admin#audit");
    clear.textContent = "Clear filters";
    form.appendChild(clear);
  }
})();

// An admin account is not tied to facilities, so the boxes are put away for that role.
(function () {
  "use strict";
  var box = document.querySelector("fieldset[data-facilities]");
  var role = box && box.form && box.form.elements.role;
  if (!role) {
    return;
  }
  function sync() {
    var off = role.value === "ADMIN";
    box.hidden = off;
    Array.prototype.forEach.call(box.querySelectorAll("input"), function (input) { input.disabled = off; });
  }
  role.addEventListener("change", sync);
  sync();
})();
