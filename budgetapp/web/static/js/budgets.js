/* Budgets page: the "Suggest" buttons.
 *
 * A Suggest button (data-suggest-target="<input id>", data-suggest="615.00") fills that amount
 * field and selects it; it never saves by itself. In a budget row, which saves when its value
 * changes, Enter saves the suggestion, and Esc or leaving the field puts the saved amount back,
 * so the row never shows an amount that isn't saved. In the "Add a budget" dialog the button
 * reads the suggestion from the chosen category (data-suggest-select="<select id>"). */
(function () {
  "use strict";

  var doc = document;

  function toast(level, text) {
    if (window.App && window.App.toast) window.App.toast(level, text);
  }

  function inlineRow(input) {
    return !!(input.form && input.form.classList.contains("budget-form"));
  }

  function revert(input) {
    if (!input.classList.contains("is-suggested")) return;
    input.classList.remove("is-suggested");
    input.value = input.defaultValue;
  }

  doc.addEventListener("click", function (evt) {
    var button = evt.target instanceof Element ? evt.target.closest("[data-suggest-target]") : null;
    if (!button) return;
    var input = doc.getElementById(button.getAttribute("data-suggest-target"));
    if (!input) return;
    var value = button.getAttribute("data-suggest");
    var selectId = button.getAttribute("data-suggest-select");
    if (selectId) {
      var select = doc.getElementById(selectId);
      var option = select && select.selectedOptions ? select.selectedOptions[0] : null;
      if (!option || !option.value) {
        toast("info", "Choose a category first.");
        if (select) select.focus();
        return;
      }
      value = option.getAttribute("data-suggest");
      if (!value) {
        toast("info", "No suggestion for that category yet: there is no complete period of spending to average.");
        return;
      }
    }
    if (!value) return;
    // The value goes in before the field takes focus, so leaving it is not a change (no save).
    input.value = value;
    if (inlineRow(input)) input.classList.add("is-suggested");
    input.focus();
    input.select();
  });

  // Editing a suggested amount makes it an ordinary edit (saved when the field is left).
  doc.addEventListener("input", function (evt) {
    var input = evt.target;
    if (input && input.classList) input.classList.remove("is-suggested");
  });

  doc.addEventListener("keydown", function (evt) {
    var input = evt.target;
    if (!input || !input.classList || !input.classList.contains("is-suggested")) return;
    if (evt.key === "Escape") {
      evt.preventDefault();
      revert(input);
    } else if (evt.key === "Enter") {
      input.classList.remove("is-suggested");
    }
  });

  doc.addEventListener("focusout", function (evt) {
    var input = evt.target;
    if (input && input.classList && input.classList.contains("is-suggested")) revert(input);
  });
})();
