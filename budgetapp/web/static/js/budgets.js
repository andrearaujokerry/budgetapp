/* Budgets page: the "Suggest" buttons.
 *
 * A Suggest button (data-suggest-target="<input id>", data-suggest="615.00") fills that amount
 * field and selects it; it never saves by itself. In a budget row the suggestion then saves
 * like a typed amount: when you leave the field or press Enter. Esc puts the saved amount back.
 * While it waits, the field is dashed and the hint under it (#<input id>-hint) says so; screen
 * readers hear the hint with the field. In the "Add a budget" dialog the button reads the
 * suggestion from the chosen category (data-suggest-select="<select id>"). */
(function () {
  "use strict";

  var doc = document;

  function toast(level, text) {
    if (window.App && window.App.toast) window.App.toast(level, text);
  }

  function inlineRow(input) {
    return !!(input.form && input.form.classList.contains("budget-form"));
  }

  function showHint(input, on) {
    var hint = doc.getElementById(input.id + "-hint");
    if (!hint) return;
    hint.hidden = !on;
    var ids = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter(function (id) {
      return id && id !== hint.id;
    });
    if (on) ids.push(hint.id);
    if (ids.length) input.setAttribute("aria-describedby", ids.join(" "));
    else input.removeAttribute("aria-describedby");
  }

  // The suggestion is no longer pending: it was saved, edited or cancelled.
  function settle(input) {
    input.classList.remove("is-suggested");
    showHint(input, false);
  }

  function revert(input) {
    if (!input.classList.contains("is-suggested")) return;
    settle(input);
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
    // The value goes in before the field takes focus, so the browser sees no "change" when the
    // field is left: the focusout handler below saves it instead.
    input.value = value;
    if (inlineRow(input)) {
      input.classList.add("is-suggested");
      showHint(input, true);
    }
    input.focus();
    input.select();
  });

  // Editing a suggested amount makes it an ordinary edit (saved when the field is left).
  doc.addEventListener("input", function (evt) {
    var input = evt.target;
    if (input && input.classList && input.classList.contains("is-suggested")) settle(input);
  });

  doc.addEventListener("keydown", function (evt) {
    var input = evt.target;
    if (!input || !input.classList || !input.classList.contains("is-suggested")) return;
    if (evt.key === "Escape") {
      evt.preventDefault();
      revert(input);
    } else if (evt.key === "Enter") {
      settle(input);
    }
  });

  // Leaving the field saves the suggestion, the same as a typed amount.
  doc.addEventListener("focusout", function (evt) {
    var input = evt.target;
    if (!input || !input.classList || !input.classList.contains("is-suggested")) return;
    settle(input);
    if (inlineRow(input) && input.value !== input.defaultValue) input.form.requestSubmit();
  });
})();
