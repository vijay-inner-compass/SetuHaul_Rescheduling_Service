// Warehouse screen: switching facility reloads the board; picking a vehicle selects its carrier.
(function () {
  "use strict";

  // Changing the facility shows that facility's board at once.
  var facility = document.querySelector("#board select[name=facility]");
  if (facility && facility.form) {
    facility.addEventListener("change", function () {
      facility.form.submit();
    });
  }

  // The add-driver form: a vehicle belongs to one carrier, so choosing it sets the carrier too.
  var vehicle = document.getElementById("vehicle");
  var carrier = document.getElementById("carrier");
  if (vehicle && carrier) {
    var sync = function () {
      var chosen = vehicle.options[vehicle.selectedIndex];
      if (chosen && chosen.getAttribute("data-carrier")) {
        carrier.value = chosen.getAttribute("data-carrier");
      }
    };
    vehicle.addEventListener("change", sync);
    sync();
  }
})();
