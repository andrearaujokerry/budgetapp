/* Transactions page (TXN-1..5). Loaded after htmx and app.js; no inline JS (CSP).
 *
 *   [popover] + [data-popover-anchor]  row menus and the category picker open under their button
 *                                      (they close on scroll, resize and when an item is chosen)
 *   [data-catpick]                     the picker's summary follows the boxes; Clear unticks all
 *   [data-split-form]                  what is left to assign, Add / Remove a part, and for a new
 *                                      split the first part keeps what the others leave; Save is
 *                                      enabled only when the parts add up to the cent
 *   [data-results][data-sort]          the filter form's hidden sort follows sort-header clicks
 *
 * Money is handled in integer cents parsed from the text, never as floats.
 */
(function () {
  "use strict";

  var doc = document;
  var forEach = function (list, fn) { Array.prototype.forEach.call(list, fn); };
  var GAP = 4;
  var EDGE = 8;

  /* ------------------------------------------------------------ popovers */

  function anchorOf(pop) {
    return pop.id ? doc.querySelector('[popovertarget="' + pop.id + '"][data-popover-anchor]') : null;
  }

  // Before it shows: under the button, right-aligned for row menus, left-aligned otherwise.
  function anchor(pop) {
    var button = anchorOf(pop);
    if (!button) return;
    var r = button.getBoundingClientRect();
    var vw = doc.documentElement.clientWidth;
    pop.classList.add("is-anchored");
    pop.style.setProperty("--pop-top", Math.round(r.bottom + GAP) + "px");
    if (pop.classList.contains("txn-menu")) {
      pop.style.setProperty("--pop-right", Math.max(EDGE, Math.round(vw - r.right)) + "px");
      pop.style.removeProperty("--pop-left");
    } else {
      pop.style.setProperty("--pop-left", Math.max(EDGE, Math.round(r.left)) + "px");
      pop.style.removeProperty("--pop-right");
    }
  }

  // Once it shows (its size is known): keep it on screen, above the button if needed.
  function fit(pop) {
    var button = anchorOf(pop);
    if (!button || !pop.classList.contains("is-anchored")) return;
    var r = button.getBoundingClientRect();
    var box = pop.getBoundingClientRect();
    var vw = doc.documentElement.clientWidth;
    var vh = window.innerHeight;
    if (box.bottom > vh - EDGE && r.top - GAP - box.height >= EDGE) {
      pop.style.setProperty("--pop-top", Math.round(r.top - GAP - box.height) + "px");
    }
    if (box.right > vw - EDGE && !pop.classList.contains("txn-menu")) {
      pop.style.setProperty("--pop-left", Math.max(EDGE, Math.round(vw - EDGE - box.width)) + "px");
    }
  }

  function openPopovers() {
    return Array.prototype.filter.call(doc.querySelectorAll("[popover].txn-popover"), function (p) {
      try { return p.matches(":popover-open"); } catch (e) { return false; }
    });
  }

  function hide(pop) {
    if (pop && typeof pop.hidePopover === "function") {
      try { pop.hidePopover(); } catch (e) { /* already hidden */ }
    }
  }

  doc.addEventListener("beforetoggle", function (evt) {
    var pop = evt.target;
    if (pop.classList && pop.classList.contains("txn-popover") && evt.newState === "open") anchor(pop);
  }, true);

  doc.addEventListener("toggle", function (evt) {
    var pop = evt.target;
    if (pop.classList && pop.classList.contains("txn-popover") && evt.newState === "open") fit(pop);
  }, true);

  // A fixed popover would drift away from its button: close it when the page moves.
  function closeOnMove(evt) {
    var t = evt && evt.target;
    openPopovers().forEach(function (pop) {
      if (t && t.nodeType === 1 && pop.contains(t)) return; // scrolling the picker's own list
      hide(pop);
    });
  }
  doc.addEventListener("scroll", closeOnMove, { capture: true, passive: true });
  window.addEventListener("resize", closeOnMove);

  /* ------------------------------------------------- row menus and focus */

  var returnTo = null; // the row whose menu opened a dialog: focus goes back to it afterwards

  function focusRowMenu(id) {
    var row = id ? doc.getElementById(id) : null;
    var button = row ? row.querySelector("[data-popover-anchor]") : null;
    if (button) button.focus();
  }

  function focusLost() {
    var a = doc.activeElement;
    return !a || a === doc.body || !a.isConnected;
  }

  doc.addEventListener("click", function (evt) {
    var item = evt.target instanceof Element ? evt.target.closest("[data-menu-item]") : null;
    if (!item) return;
    var row = item.closest("tr");
    returnTo = row ? row.id : null;
    hide(item.closest("[popover]"));
  });

  doc.addEventListener("close", function (evt) {
    if (!(evt.target instanceof HTMLDialogElement) || !returnTo) return;
    var id = returnTo;
    window.setTimeout(function () { if (focusLost()) focusRowMenu(id); }, 0);
  }, true);

  /* ---------------------------------------------------- category picker */

  function pickerSummary(picker) {
    var names = [];
    forEach(picker.querySelectorAll('input[type="checkbox"]:checked'), function (box) {
      names.push(box.getAttribute("data-label") || box.value);
    });
    if (!names.length) return "All categories";
    return names.length === 1 ? names[0] : names[0] + " +" + (names.length - 1);
  }

  function refreshPicker(picker) {
    var field = picker.closest(".txn-catpick");
    var out = field ? field.querySelector("[data-catpick-summary]") : null;
    if (out) out.textContent = pickerSummary(picker);
  }

  doc.addEventListener("change", function (evt) {
    var picker = evt.target instanceof Element ? evt.target.closest("[data-catpick]") : null;
    if (picker) refreshPicker(picker);
  });

  doc.addEventListener("click", function (evt) {
    var clear = evt.target instanceof Element ? evt.target.closest("[data-catpick-clear]") : null;
    if (!clear) return;
    var picker = clear.closest("[data-catpick]");
    var boxes = picker ? picker.querySelectorAll('input[type="checkbox"]:checked') : [];
    if (!boxes.length) return;
    forEach(boxes, function (box) { box.checked = false; });
    refreshPicker(picker);
    // One change event: app.js submits the filter form (data-autosubmit).
    boxes[0].dispatchEvent(new Event("change", { bubbles: true }));
  });

  /* ------------------------------------------------------ split editor */

  var MINUS = "−";

  // "1,234.5" → 123450; "" → null; anything else → NaN. Exact: no floats involved.
  function toCents(text) {
    var s = String(text == null ? "" : text).replace(/[\s$,]/g, "").replace(MINUS, "-");
    if (s === "") return null;
    var m = /^(\d*)(?:\.(\d{0,2}))?$/.exec(s);
    if (!m || (m[1] === "" && !m[2])) return NaN;
    return (m[1] ? parseInt(m[1], 10) : 0) * 100 + parseInt(((m[2] || "") + "00").slice(0, 2), 10);
  }

  function plain(cents) {
    return Math.floor(cents / 100) + "." + ("0" + (cents % 100)).slice(-2);
  }

  function money(cents) {
    var whole = String(Math.floor(Math.abs(cents) / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (cents < 0 ? MINUS : "") + "$" + whole + "." + ("0" + (Math.abs(cents) % 100)).slice(-2);
  }

  function parts(form) {
    return Array.prototype.slice.call(form.querySelectorAll("[data-split-parts] > [data-split-part]"));
  }

  function amountInput(part) {
    return part.querySelector("[data-split-amount]");
  }

  function renumber(form) {
    parts(form).forEach(function (part, i) {
      forEach(part.querySelectorAll(".field__label"), function (label) {
        label.textContent = label.textContent.replace(/^Part \S+/, "Part " + (i + 1));
      });
      var remove = part.querySelector("[data-split-remove]");
      if (remove) remove.setAttribute("aria-label", "Remove part " + (i + 1));
    });
  }

  function updateSplit(form) {
    var total = toCents(form.getAttribute("data-total"));
    var list = parts(form);
    var inputs = list.map(amountInput);
    if (form._autoFirst && inputs[0]) {
      var others = 0;
      var known = true;
      inputs.slice(1).forEach(function (input) {
        var c = toCents(input && input.value);
        if (c === null) return;
        if (isNaN(c)) known = false;
        else others += c;
      });
      if (known) inputs[0].value = total - others > 0 ? plain(total - others) : "";
    }
    var sum = 0;
    var filled = 0;
    var bad = false;
    inputs.forEach(function (input) {
      var c = toCents(input && input.value);
      if (c === null) return;
      if (isNaN(c) || c === 0) bad = true;
      else {
        sum += c;
        filled += 1;
      }
    });
    var left = total - sum;
    var credit = form.getAttribute("data-kind") === "credit";
    var state;
    var text;
    if (bad) {
      state = "over";
      text = "Check the amounts: use numbers like 12.50, without a minus sign.";
    } else if (filled < 2) {
      state = "warn";
      text = left > 0 ? money(left) + " left to assign. Use at least two parts." : "Use at least two parts.";
    } else if (left === 0) {
      state = "ok";
      text = "✓ The parts add up to " + money(total) + ".";
    } else if (left > 0) {
      state = "warn";
      text = money(left) + " left to assign.";
    } else {
      state = "over";
      text = money(-left) + " more than the " + (credit ? "credit" : "charge") + ".";
    }
    var out = form.querySelector("[data-split-remaining]");
    if (out) {
      out.textContent = text;
      out.setAttribute("data-state", state);
    }
    var save = form.querySelector("[data-split-save]");
    if (save) save.disabled = state !== "ok";
    var add = form.querySelector("[data-split-add]");
    var max = parseInt(form.getAttribute("data-max-parts"), 10) || 12;
    if (add) add.hidden = list.length >= max;
  }

  function addPart(form) {
    var template = form.querySelector("template[data-split-template]");
    var list = form.querySelector("[data-split-parts]");
    if (!template || !list) return;
    form._nextIndex = (form._nextIndex || parts(form).length) + 1;
    var holder = doc.createElement("template");
    holder.innerHTML = template.innerHTML
      .replace(/__i__/g, "n" + form._nextIndex)
      .replace(/__n__/g, String(parts(form).length + 1));
    var part = holder.content.firstElementChild;
    if (!part) return;
    list.appendChild(part);
    renumber(form);
    updateSplit(form);
    var select = part.querySelector("select");
    if (select) select.focus();
  }

  function removePart(form, part) {
    var list = parts(form);
    if (list.indexOf(part) === 0) form._autoFirst = false;
    var next = list[list.indexOf(part) + 1] || list[list.indexOf(part) - 1];
    part.parentNode.removeChild(part);
    renumber(form);
    updateSplit(form);
    var focus = next && next.isConnected ? next.querySelector("[data-split-remove]") : form.querySelector("[data-split-add]");
    if (focus) focus.focus();
  }

  function initSplits(root) {
    forEach((root || doc).querySelectorAll("[data-split-form]"), function (form) {
      if (form._splitReady) return;
      form._splitReady = true;
      form._autoFirst = form.hasAttribute("data-auto-first");
      updateSplit(form);
    });
  }

  doc.addEventListener("input", function (evt) {
    var input = evt.target instanceof Element ? evt.target.closest("[data-split-amount]") : null;
    var form = input ? input.closest("[data-split-form]") : null;
    if (!form) return;
    if (form._autoFirst && parts(form)[0] && parts(form)[0].contains(input)) form._autoFirst = false;
    updateSplit(form);
  });

  doc.addEventListener("click", function (evt) {
    var t = evt.target instanceof Element ? evt.target : null;
    if (!t) return;
    var add = t.closest("[data-split-add]");
    var remove = t.closest("[data-split-remove]");
    var form = (add || remove) ? (add || remove).closest("[data-split-form]") : null;
    if (!form) return;
    evt.preventDefault();
    if (add) addPart(form);
    else removePart(form, remove.closest("[data-split-part]"));
  });

  /* --------------------------------------------------- after HTMX swaps */

  function syncSort() {
    var results = doc.querySelector("[data-results]");
    var input = doc.querySelector("#txn-filters [data-sort-input]");
    if (results && input) input.value = results.getAttribute("data-sort") || "";
  }

  doc.addEventListener("htmx:afterSettle", function (evt) {
    var el = evt.target;
    initSplits(el && el.querySelectorAll ? el : doc);
    syncSort();
    if (el && el.matches && el.matches("tr.txn-row") && returnTo === el.id && focusLost()) {
      focusRowMenu(el.id);
    }
  });

  function init() {
    initSplits(doc);
    syncSort();
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
  else init();
})();
