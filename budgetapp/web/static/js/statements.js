/* Statements page: the Print button (inline handlers are blocked by the CSP). */
(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-print]");
    if (!button) {
      return;
    }
    event.preventDefault();
    window.print();
  });
})();
