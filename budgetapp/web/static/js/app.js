/* Budget Tracker: shared behaviour for every page.
 *
 * Loaded with `defer` after htmx (and Chart.js on pages that set the `charts`
 * block). The CSP forbids inline JavaScript, so everything hangs off data-*
 * attributes; the vocabulary is documented in templates/components/README.md.
 *
 *   data-chart="<id>"          <canvas> drawn from <script type="application/json" id="<id>-config">
 *   data-dialog-open="<id>"    opens <dialog id="<id>">;  data-dialog-close closes the enclosing dialog
 *   data-dialog-autoopen       <dialog> opens as soon as it appears (e.g. returned by htmx)
 *   data-confirm="Question?"   asks before the form/button/link submits or sends its htmx request
 *   data-autosubmit            <form> submits on change; typing and date entry are debounced (400 ms)
 *   data-search                "/" focuses it
 *   data-tags-input            tag chips editor (forms.tags_field)
 *   data-toast / data-dismiss  flash messages: ok/info/warn auto-dismiss after 6 s, errors stay; close button
 *   data-flash                 swapped-in element glows briefly (swapped <tr> rows always do)
 *   .table-wrap                gets .is-fit (table fits: header sticks to the page) or .is-scrollable
 *
 * Page scripts can use window.App: toast(level, text), renderCharts(force),
 * formatMoney(number).
 */
(function () {
  "use strict";

  var doc = document;
  var root = doc.documentElement;
  var TOAST_MS = 6000;
  var TYPE_DEBOUNCE_MS = 400;
  var CHART_SRC = "/static/vendor/chart.umd.min.js";
  var forEach = function (list, fn) { Array.prototype.forEach.call(list, fn); };

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  /* ------------------------------------------------------------------ htmx */

  function configureHtmx() {
    var h = window.htmx;
    if (!h) return;
    h.config.allowEval = false;                // CSP: no eval, no hx-on, no js: expressions
    h.config.includeIndicatorStyles = false;   // CSP: no injected <style>; rules live in app.css
    // allowScriptTags stays true on purpose: when false, htmx deletes EVERY <script> in
    // swapped HTML, including the <script type="application/json"> chart configs. Inline
    // JavaScript in a swap is blocked by the CSP anyway; never put <script src> in partials.
    h.config.historyCacheSize = 0;             // never copy page HTML (financial data) into browser storage
    h.config.refreshOnHistoryMiss = true;      // so Back/Forward does a normal full page load
    h.config.selfRequestsOnly = true;
    h.config.responseHandling = [
      { code: "204", swap: false },
      { code: "[23]..", swap: true },
      { code: "422", swap: true },             // validation errors: swap the re-rendered form
      { code: "[45]..", swap: false, error: true },
      { code: "...", swap: false }
    ];
  }
  configureHtmx();

  function isQuiet(elt) {
    if (!elt || !elt.getAttribute) return false;
    return elt.hasAttribute("data-quiet") || /\bevery\b/.test(elt.getAttribute("hx-trigger") || "");
  }

  // Busy state is keyed on the request's XHR: htmx can fire afterRequest twice for one
  // request (again on a parent when the element replaced itself), and a response can
  // retarget, so detail.target is not reliable at the end.
  var inFlight = 0;
  doc.addEventListener("htmx:beforeRequest", function (evt) {
    var d = evt.detail || {};
    if (evt.defaultPrevented || !d.xhr || isQuiet(d.elt)) return;
    var el = d.target && d.target.setAttribute ? d.target : null;
    if (el) el.setAttribute("aria-busy", "true");
    d.xhr._busy = { el: el, done: false };
    inFlight += 1;
    root.classList.add("is-loading");
  });
  doc.addEventListener("htmx:afterRequest", function (evt) {
    var d = evt.detail || {};
    var busy = d.xhr && d.xhr._busy;
    if (!busy || busy.done) return;
    busy.done = true;
    if (busy.el) busy.el.removeAttribute("aria-busy");
    inFlight = Math.max(0, inFlight - 1);
    if (!inFlight) root.classList.remove("is-loading");
  });

  // Polling (hx-trigger="every …") and data-quiet requests never raise toasts: a
  // stopped app would otherwise add an alert every few seconds.
  doc.addEventListener("htmx:responseError", function (evt) {
    if (isQuiet(evt.detail && evt.detail.elt)) return;
    var xhr = evt.detail && evt.detail.xhr;
    var status = xhr ? xhr.status : 0;
    var config = evt.detail && evt.detail.requestConfig;
    var verb = String((config && config.verb) || "get").toLowerCase();
    var text;
    if (status === 403) text = "That action was blocked by the app's security check. Reload the page and try again.";
    else if (status === 404 && verb !== "get") text = "That action isn't available. Reload the page and try again.";
    else if (status === 404) text = "That item no longer exists. Reload the page to see the latest data.";
    else if (status === 409) text = "Something changed in the meantime. Reload the page and try again.";
    else if (status >= 500) text = "Something went wrong (" + status + "). Nothing was saved; the details are in the app's log.";
    else text = "The request failed (" + status + ").";
    toast("error", text);
  });
  doc.addEventListener("htmx:sendError", function (evt) {
    if (isQuiet(evt.detail && evt.detail.elt)) return;
    toast("error", "Can't reach the app. Is it still running?");
  });

  /* ---------------------------------------------------------------- toasts */

  function svgIcon(name, cls) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = doc.createElementNS(ns, "svg");
    svg.setAttribute("class", "icon" + (cls ? " " + cls : ""));
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var use = doc.createElementNS(ns, "use");
    use.setAttribute("href", "#i-" + name);
    svg.appendChild(use);
    return svg;
  }

  var TONES = { success: "ok", ok: "ok", info: "info", warning: "warn", warn: "warn", error: "over", danger: "over", over: "over" };
  var TONE_ICONS = { ok: "check-circle", info: "info", warn: "alert", over: "x-circle" };

  // Errors stay until closed (data-persist); the others auto-dismiss. A toast that
  // is already showing with the same text is not added twice.
  function toast(level, text) {
    var stack = doc.getElementById("flash");
    if (!stack) return null;
    var tone = TONES[String(level).toLowerCase()] || "info";
    var message = String(text);
    var same = Array.prototype.filter.call(stack.querySelectorAll("[data-toast]:not(.is-leaving)"), function (t) {
      var body = t.querySelector(".toast__text");
      return t.classList.contains("toast--" + tone) && body && body.textContent === message;
    })[0];
    if (same) return same;
    var el = doc.createElement("div");
    el.className = "toast toast--" + tone;
    el.setAttribute("data-toast", "");
    if (tone === "over") {
      el.setAttribute("role", "alert");
      el.setAttribute("data-persist", "");
    }
    el.appendChild(svgIcon(TONE_ICONS[tone], "toast__icon"));
    var body = doc.createElement("div");
    body.className = "toast__text";
    body.textContent = message;
    el.appendChild(body);
    var close = doc.createElement("button");
    close.type = "button";
    close.className = "btn btn--ghost btn--icon btn--sm toast__close";
    close.setAttribute("data-dismiss", "");
    close.setAttribute("aria-label", "Dismiss");
    close.appendChild(svgIcon("x"));
    el.appendChild(close);
    stack.appendChild(el);
    return el;
  }

  function dismiss(el) {
    if (!el || !el.parentNode || el.classList.contains("is-leaving")) return;
    el.classList.add("is-leaving");
    window.setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, reducedMotion() ? 0 : 220);
  }

  function armToast(el) {
    if (el._armed || el.hasAttribute("data-persist")) return;
    el._armed = true;
    var timer = null;
    var remaining = TOAST_MS;
    var started = 0;
    function start() {
      if (timer) return;
      started = Date.now();
      timer = window.setTimeout(function () { dismiss(el); }, remaining);
    }
    function pause() {
      if (!timer) return;
      window.clearTimeout(timer);
      timer = null;
      remaining = Math.max(1500, remaining - (Date.now() - started));
    }
    el.addEventListener("mouseenter", pause);
    el.addEventListener("mouseleave", start);
    el.addEventListener("focusin", pause);
    el.addEventListener("focusout", function (e) {
      if (!el.contains(e.relatedTarget)) start();
    });
    start();
  }

  // Toasts that arrive in the page HTML (e.g. "Rule saved." after a POST -> 303 ->
  // GET) are not announced by the live region, which only reads later changes, so
  // their text is repeated once in a visually hidden live region.
  function announceInitial(stack) {
    var texts = Array.prototype.map.call(stack.querySelectorAll("[data-toast] .toast__text"), function (t) {
      return t.textContent.trim();
    }).filter(Boolean);
    if (!texts.length) return;
    var live = doc.createElement("div");
    live.className = "sr-only";
    live.setAttribute("aria-live", stack.querySelector("[data-toast][role=alert]") ? "assertive" : "polite");
    doc.body.appendChild(live);
    window.setTimeout(function () { live.textContent = texts.join(" "); }, 150);
    window.setTimeout(function () { if (live.parentNode) live.parentNode.removeChild(live); }, 8000);
  }

  function watchFlash() {
    var stack = doc.getElementById("flash");
    if (!stack) return;
    announceInitial(stack);
    forEach(stack.querySelectorAll("[data-toast]"), armToast);
    if (!window.MutationObserver) return;
    new MutationObserver(function (records) {
      records.forEach(function (r) {
        forEach(r.addedNodes, function (n) {
          if (n.nodeType !== 1) return;
          if (n.matches("[data-toast]")) armToast(n);
          forEach(n.querySelectorAll("[data-toast]"), armToast);
        });
      });
    }).observe(stack, { childList: true, subtree: true });
  }

  /* --------------------------------------------------------------- dialogs */

  function openDialog(dlg) {
    if (!dlg || dlg.open || typeof dlg.showModal !== "function") return;
    dlg.showModal();
  }

  function openAutoDialogs() {
    forEach(doc.querySelectorAll("dialog[data-dialog-autoopen]"), function (dlg) {
      dlg.removeAttribute("data-dialog-autoopen");
      openDialog(dlg);
    });
  }

  function closeDialogs(from) {
    var dlg = from && from.closest ? from.closest("dialog[open]") : null;
    if (dlg) dlg.close();
    else forEach(doc.querySelectorAll("dialog[open]"), function (d) { d.close(); });
  }

  // Server-driven close: respond with the header  HX-Trigger: dialog:close
  doc.addEventListener("dialog:close", function (evt) { closeDialogs(evt.target); });

  // Backdrop click closes, but only when the press also started on the backdrop
  // (so selecting text and releasing outside does not close the dialog).
  var pressedBackdrop = null;
  function onBackdrop(evt, dlg) {
    var r = dlg.getBoundingClientRect();
    return evt.clientX < r.left || evt.clientX > r.right || evt.clientY < r.top || evt.clientY > r.bottom;
  }
  doc.addEventListener("pointerdown", function (evt) {
    var t = evt.target;
    pressedBackdrop = (t instanceof HTMLDialogElement && t.open && onBackdrop(evt, t)) ? t : null;
  });

  /* ------------------------------------------------------------ click hub */

  doc.addEventListener("click", function (evt) {
    var t = evt.target;
    if (!(t instanceof Element)) return;

    if (t instanceof HTMLDialogElement && t.open && pressedBackdrop === t && onBackdrop(evt, t)) {
      t.close();
      return;
    }

    var opener = t.closest("[data-dialog-open]");
    if (opener) {
      var dlg = doc.getElementById(opener.getAttribute("data-dialog-open"));
      if (dlg) {
        if (opener.tagName === "A" || (opener.type === "submit" && opener.form)) evt.preventDefault();
        openDialog(dlg);
      }
    }

    var closer = t.closest("[data-dialog-close]");
    if (closer) {
      var d = closer.closest("dialog");
      if (d) {
        evt.preventDefault();
        d.close();
      }
    }

    var dismisser = t.closest("[data-dismiss]");
    if (dismisser) dismiss(dismisser.closest("[data-toast], [data-dismissible]"));

    var remover = t.closest("[data-tag-remove]");
    if (remover) {
      evt.preventDefault();
      removeTag(remover.closest("[data-tag]"));
    }

    var box = t.closest("[data-tags-input]");
    if (box && t === box) {
      var entry = box.querySelector(".tags-input__entry");
      if (entry) entry.focus();
    }

    // CSS-only mobile menu: also close it when clicking outside or following a link.
    forEach(doc.querySelectorAll("details.mobile-nav[open]"), function (menu) {
      if (!menu.contains(t) || t.closest("a")) menu.removeAttribute("open");
    });
  });

  /* ---------------------------------------------------------- confirmation */

  function isHtmxDriven(el) {
    if (!el || !el.getAttribute) return false;
    var verbs = ["hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete"];
    for (var i = 0; i < verbs.length; i++) {
      if (el.hasAttribute(verbs[i]) || el.hasAttribute("data-" + verbs[i])) return true;
    }
    return !!(el.closest && el.closest("[hx-boost='true'], [data-hx-boost='true']"));
  }

  // Plain (non-htmx) forms: ask before submitting.
  doc.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!(form instanceof HTMLFormElement)) return;
    clearAutosubmit(form);
    var submitter = evt.submitter || null;
    if (isHtmxDriven(form) || isHtmxDriven(submitter)) return; // handled in htmx:confirm
    var question = (submitter && submitter.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
    if (question && !window.confirm(question)) {
      evt.preventDefault();
      evt.stopImmediatePropagation();
    }
  }, true);

  // Plain links with data-confirm.
  doc.addEventListener("click", function (evt) {
    var link = evt.target instanceof Element ? evt.target.closest("a[data-confirm]") : null;
    if (!link || isHtmxDriven(link)) return;
    if (!window.confirm(link.getAttribute("data-confirm"))) evt.preventDefault();
  }, true);

  // htmx requests: the element that makes the request carries data-confirm
  // (or the submit button that triggered a form request).
  doc.addEventListener("htmx:confirm", function (evt) {
    var d = evt.detail || {};
    var elt = d.elt;
    if (!elt || !elt.getAttribute) return;
    var trigger = d.triggeringEvent;
    var submitter = trigger && trigger.submitter;
    var question = (submitter && submitter.getAttribute && submitter.getAttribute("data-confirm")) || elt.getAttribute("data-confirm");
    if (!question) return;
    evt.preventDefault();
    if (window.confirm(question)) d.issueRequest(true);
  });

  /* ------------------------------------------------------------ autosubmit */

  var timers = typeof WeakMap === "function" ? new WeakMap() : null;

  function isTextual(el) {
    if (!el || !el.tagName) return false;
    if (el.tagName === "TEXTAREA") return true;
    return el.tagName === "INPUT" && /^(text|search|email|url|tel|number|password)$/.test(el.type);
  }

  // Typing a year into a date field fires "change" for every intermediate valid
  // value (0002, 0020, 0202, 2026), so these are debounced like text.
  function isDateLike(el) {
    return !!el && el.tagName === "INPUT" && /^(date|month|week|time|datetime-local)$/.test(el.type);
  }

  function clearAutosubmit(form) {
    if (!timers) return;
    var t = timers.get(form);
    if (t) {
      window.clearTimeout(t);
      timers.delete(form);
    }
  }

  function submitForm(form) {
    if (!form.isConnected) return;
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.submit();
  }

  function autosubmit(form, delay) {
    clearAutosubmit(form);
    if (!delay || !timers) {
      submitForm(form);
      return;
    }
    timers.set(form, window.setTimeout(function () {
      timers.delete(form);
      submitForm(form);
    }, delay));
  }

  function formOf(el) {
    return el ? (el.form || (el.closest ? el.closest("form") : null)) : null;
  }

  doc.addEventListener("input", function (evt) {
    var el = evt.target;
    var form = formOf(el);
    if (!form || !form.hasAttribute("data-autosubmit") || !isTextual(el)) return;
    if (el.classList.contains("tags-input__entry")) return; // tags save when a chip is added
    autosubmit(form, TYPE_DEBOUNCE_MS);
  });

  doc.addEventListener("change", function (evt) {
    var el = evt.target;
    if (el.matches && el.matches("[data-category-select]")) updateCategoryDot(el);
    var form = formOf(el);
    if (!form || !form.hasAttribute("data-autosubmit") || isTextual(el)) return;
    if (el.classList && el.classList.contains("tags-input__entry")) return;
    autosubmit(form, isDateLike(el) ? TYPE_DEBOUNCE_MS : 0);
  });

  // Enter in a text field submits an autosubmit form at once. (Browsers skip implicit
  // submission when a form has several text/date fields and no submit button.)
  doc.addEventListener("keydown", function (evt) {
    if (evt.key !== "Enter" || evt.isComposing || evt.defaultPrevented) return;
    var el = evt.target;
    if (!(isTextual(el) || isDateLike(el)) || el.tagName === "TEXTAREA" || el.classList.contains("tags-input__entry")) return;
    var form = formOf(el);
    if (!form || !form.hasAttribute("data-autosubmit")) return;
    evt.preventDefault();
    autosubmit(form, 0);
  });

  function updateCategoryDot(select) {
    var wrap = select.closest(".inline-cat");
    if (!wrap) return;
    var opt = select.options[select.selectedIndex];
    var color = opt ? opt.getAttribute("data-color") : null;
    if (color) wrap.style.setProperty("--cat-color", color);
    else wrap.style.removeProperty("--cat-color");
  }

  /* -------------------------------------------------------------- tags */

  function tagValues(box) {
    var out = [];
    forEach(box.querySelectorAll("[data-tag] input[type=hidden]"), function (i) { out.push(i.value.toLowerCase()); });
    return out;
  }

  function makeChip(name, tag) {
    var chip = doc.createElement("span");
    chip.className = "chip chip--removable";
    chip.setAttribute("data-tag", "");
    var label = doc.createElement("span");
    label.textContent = tag;
    chip.appendChild(label);
    var hidden = doc.createElement("input");
    hidden.type = "hidden";
    hidden.name = name;
    hidden.value = tag;
    chip.appendChild(hidden);
    var btn = doc.createElement("button");
    btn.type = "button";
    btn.className = "chip__remove";
    btn.setAttribute("data-tag-remove", "");
    btn.setAttribute("aria-label", "Remove tag " + tag);
    btn.appendChild(svgIcon("x"));
    chip.appendChild(btn);
    return chip;
  }

  function fireChange(el) {
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function addTags(box, raw) {
    var entry = box.querySelector(".tags-input__entry");
    var name = box.getAttribute("data-name") || "tags";
    var seen = tagValues(box);
    var added = false;
    String(raw).split(",").forEach(function (part) {
      var tag = part.replace(/\s+/g, " ").trim();
      if (!tag || seen.indexOf(tag.toLowerCase()) !== -1) return;
      seen.push(tag.toLowerCase());
      box.insertBefore(makeChip(name, tag), entry);
      added = true;
    });
    if (added) fireChange(box);
  }

  function removeTag(chip) {
    if (!chip) return;
    var box = chip.closest("[data-tags-input]");
    chip.parentNode.removeChild(chip);
    if (!box) return;
    fireChange(box);
    var entry = box.querySelector(".tags-input__entry");
    if (entry) entry.focus();
  }

  function commitEntry(entry) {
    var box = entry.closest("[data-tags-input]");
    if (!box || !entry.value.trim()) return;
    addTags(box, entry.value);
    entry.value = "";
  }

  doc.addEventListener("keydown", function (evt) {
    var entry = evt.target;
    if (!entry.classList || !entry.classList.contains("tags-input__entry") || evt.isComposing) return;
    if ((evt.key === "Enter" || evt.key === ",") && entry.value.trim()) {
      evt.preventDefault();
      commitEntry(entry);
    } else if (evt.key === "Backspace" && !entry.value) {
      var box = entry.closest("[data-tags-input]");
      var chips = box ? box.querySelectorAll("[data-tag]") : [];
      if (chips.length) {
        evt.preventDefault();
        removeTag(chips[chips.length - 1]);
      }
    }
  });

  doc.addEventListener("focusout", function (evt) {
    var entry = evt.target;
    if (entry.classList && entry.classList.contains("tags-input__entry")) commitEntry(entry);
  });

  /* ---------------------------------------------------- keyboard shortcuts */

  function visible(el) {
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }

  doc.addEventListener("keydown", function (evt) {
    if (evt.key === "Escape") {
      forEach(doc.querySelectorAll("details.mobile-nav[open]"), function (m) { m.removeAttribute("open"); });
      return;
    }
    if (evt.key !== "/" || evt.ctrlKey || evt.metaKey || evt.altKey || evt.defaultPrevented) return;
    var a = doc.activeElement;
    if (a && (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))) return;
    if (doc.querySelector("dialog[open]")) return;
    var target = Array.prototype.filter.call(doc.querySelectorAll("[data-search]"), visible)[0];
    if (!target) return;
    evt.preventDefault();
    target.focus();
    if (typeof target.select === "function") target.select();
  });

  /* ---------------------------------------------------------------- charts */

  var charts = typeof Map === "function" ? new Map() : null; // canvas -> Chart
  var chartLoader = null;

  function ensureChartJs() {
    if (window.Chart) return Promise.resolve(window.Chart);
    if (chartLoader) return chartLoader;
    chartLoader = new Promise(function (resolve, reject) {
      var s = doc.createElement("script");
      s.src = CHART_SRC;
      s.addEventListener("load", function () { resolve(window.Chart); });
      s.addEventListener("error", function () {
        chartLoader = null;
        reject(new Error("Chart.js failed to load"));
      });
      doc.head.appendChild(s);
    });
    return chartLoader;
  }

  function cssVar(name) {
    return getComputedStyle(root).getPropertyValue(name).trim();
  }

  function readTheme() {
    var series = [];
    for (var i = 1; i <= 16; i++) series.push(cssVar("--cat-" + i));
    return {
      text: cssVar("--text"),
      muted: cssVar("--text-muted"),
      grid: cssVar("--chart-grid"),
      axis: cssVar("--chart-axis"),
      surface: cssVar("--surface"),
      other: cssVar("--chart-other"),
      tooltipBg: cssVar("--tooltip-bg"),
      tooltipText: cssVar("--tooltip-text"),
      font: cssVar("--font-sans") || "system-ui, sans-serif",
      series: series,
      named: {
        accent: cssVar("--chart-accent"),
        ok: cssVar("--ok"),
        warn: cssVar("--warn"),
        over: cssVar("--over"),
        info: cssVar("--info"),
        muted: cssVar("--text-muted"),
        other: cssVar("--chart-other")
      }
    };
  }

  // Colour names a config may use (the same forms as ui.color_value(), so a
  // category's stored colour works as is): "cat-1".."cat-16" (palette slot for
  // the current theme), "other" (neutral grey), "#rgb"/"#rrggbb" (custom), plus
  // "accent" | "ok" | "warn" | "over" | "info" | "muted", "--any-token" /
  // "var(--any-token)" or another literal CSS colour. Case and spaces are
  // ignored for the names. Missing colours take the categorical slot for their
  // index; there is no 17th hue (index >= 16, or "cat-17", gets "other").
  // Literal hex colours are nudged lighter (dark theme) or darker (light theme)
  // until they reach 3:1 against the card surface, so a dark red stays visible.
  function resolveColor(value, theme, index) {
    if (value === null || value === undefined || String(value).trim() === "") {
      return index < theme.series.length ? theme.series[index] : theme.other;
    }
    var v = String(value).trim();
    var key = v.toLowerCase();
    var m = /^cat-(\d{1,2})$/.exec(key);
    if (m) return theme.series[(+m[1] - 1)] || theme.other;
    if (Object.prototype.hasOwnProperty.call(theme.named, key)) return theme.named[key];
    m = /^var\((--[\w-]+)\)$/.exec(v);
    if (m) return cssVar(m[1]) || theme.other;
    if (/^--[\w-]+$/.test(v)) return cssVar(v) || theme.other;
    return legible(v, theme.surface);
  }

  function hexRgb(color) {
    var m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color);
    if (!m) return null;
    var h = m[1].length === 3 ? m[1].replace(/./g, "$&$&") : m[1];
    var n = parseInt(h, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function luminance(rgb) {
    var c = rgb.map(function (v) {
      v /= 255;
      return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }

  function contrast(a, b) {
    var x = luminance(a), y = luminance(b);
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
  }

  function legible(color, surface) {
    var rgb = hexRgb(color), bg = hexRgb(surface);
    if (!rgb || !bg || contrast(rgb, bg) >= 3) return color;
    var toward = luminance(bg) < 0.2 ? 255 : 0;
    for (var t = 0.05; t < 0.951; t += 0.05) {
      var mixed = rgb.map(function (v) { return Math.round(v + (toward - v) * t); });
      if (contrast(mixed, bg) >= 3) {
        return "#" + mixed.map(function (v) { return ("0" + v.toString(16)).slice(-2); }).join("");
      }
    }
    return color;
  }

  function withAlpha(color, alpha) {
    var m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color);
    if (m) {
      var h = m[1].length === 3 ? m[1].replace(/./g, "$&$&") : m[1];
      var n = parseInt(h, 16);
      return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + alpha + ")";
    }
    m = /^rgba?\(([^)]+)\)$/i.exec(color);
    if (m) {
      var p = m[1].split(/[\s,/]+/).filter(Boolean);
      return "rgba(" + p[0] + "," + p[1] + "," + p[2] + "," + alpha + ")";
    }
    return color;
  }

  var formatters = {};
  function nf(key, opts) {
    if (!formatters[key]) formatters[key] = new Intl.NumberFormat("en-US", opts);
    return formatters[key];
  }

  function minus(s) {
    return s.replace("-", "−");
  }

  function formatValue(v, format, axis) {
    if (v === null || v === undefined || isNaN(v)) return "";
    switch (format) {
      case "percent":
        return minus(nf("pct", { maximumFractionDigits: axis ? 0 : 1 }).format(v)) + "%";
      case "number":
        return minus(axis
          ? nf("num-c", { notation: "compact", maximumFractionDigits: 1 }).format(v)
          : nf("num", { maximumFractionDigits: 2 }).format(v));
      case "usd0":
        return minus(axis ? usdAxis(v) : nf("usd0", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(v));
      default:
        return minus(axis ? usdAxis(v) : nf("usd", { style: "currency", currency: "USD" }).format(v));
    }
  }

  function usdAxis(v) {
    return Math.abs(v) >= 1000
      ? nf("usd-c", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 }).format(v)
      : nf("usd0", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(v);
  }

  function toNumber(v) {
    if (typeof v === "string" && v.trim() !== "" && !isNaN(+v)) return +v;
    return v;
  }

  function coerceData(data) {
    return (data || []).map(function (v) {
      if (v && typeof v === "object" && !Array.isArray(v)) {
        var o = {};
        Object.keys(v).forEach(function (k) { o[k] = toNumber(v[k]); });
        return o;
      }
      return toNumber(v);
    });
  }

  function isPlain(o) {
    return !!o && typeof o === "object" && !Array.isArray(o);
  }

  function merge(target, src) {
    if (!isPlain(src)) return target;
    Object.keys(src).forEach(function (k) {
      if (k === "__proto__" || k === "constructor" || k === "prototype") return;
      var v = src[k];
      if (isPlain(v) && isPlain(target[k])) merge(target[k], v);
      else target[k] = isPlain(v) ? merge({}, v) : v;
    });
    return target;
  }

  function crosshairPlugin(color) {
    return {
      id: "crosshair",
      afterDatasetsDraw: function (chart) {
        var active = chart.tooltip && chart.tooltip.getActiveElements ? chart.tooltip.getActiveElements() : [];
        if (!active.length) return;
        var x = active[0].element.x;
        var area = chart.chartArea;
        var ctx = chart.ctx;
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(x, area.top);
        ctx.lineTo(x, area.bottom);
        ctx.lineWidth = 1;
        ctx.strokeStyle = color;
        ctx.stroke();
        ctx.restore();
      }
    };
  }

  // Turn the JSON contract (see README "Charts") into a Chart.js configuration.
  function buildConfig(spec, theme, canvas) {
    var type = spec.type || "bar";
    var format = spec.format || "usd";
    var horizontal = !!spec.horizontal;
    var stacked = !!spec.stacked;
    var spark = !!spec.sparkline;
    var circular = type === "doughnut" || type === "pie";
    var sets = (spec.data && spec.data.datasets) || [];
    var datasets = sets.map(function (ds, i) {
      var out = {};
      Object.keys(ds).forEach(function (k) {
        if (k !== "color" && k !== "colors" && k !== "dashed") out[k] = ds[k];
      });
      out.data = coerceData(ds.data);
      var dtype = ds.type || type;
      if (circular) {
        out.backgroundColor = out.data.map(function (_, j) {
          return resolveColor(ds.colors ? ds.colors[j] : null, theme, j);
        });
        out.borderColor = theme.surface;
        out.borderWidth = 2;
        out.hoverOffset = 4;
        return out;
      }
      var color = resolveColor(ds.color, theme, i);
      if (dtype === "line") {
        out.borderColor = color;
        out.backgroundColor = withAlpha(color, 0.1);
        out.borderWidth = 2;
        out.borderCapStyle = "round";
        out.borderJoinStyle = "round";
        out.pointRadius = spark ? 0 : (out.data.length <= 1 ? 4 : 0);
        out.pointHoverRadius = spark ? 0 : 5;
        out.pointBackgroundColor = color;
        out.pointBorderColor = theme.surface;
        out.pointBorderWidth = 2;
        out.pointHitRadius = 12;
        out.tension = 0.25;
        if (out.fill === undefined) out.fill = spark ? "origin" : false;
        if (ds.dashed) out.borderDash = [5, 4];
      } else {
        var fills = ds.colors ? ds.colors.map(function (c, j) { return resolveColor(c, theme, j); }) : color;
        out.backgroundColor = fills;
        out.hoverBackgroundColor = fills;
        out.borderSkipped = "start";
        out.borderRadius = stacked ? (i === sets.length - 1 ? 4 : 0) : 4;
        out.maxBarThickness = 28;
        out.categoryPercentage = 0.72;
        out.barPercentage = 0.9;
        if (stacked) {
          out.borderColor = theme.surface;
          out.borderWidth = horizontal ? { right: 2 } : { top: 2 };
        } else {
          out.borderWidth = 0;
        }
      }
      return out;
    });

    var showLegend = spec.legend === undefined ? (!spark && (circular || sets.length > 1)) : !!spec.legend;
    var valueAxis = horizontal ? "x" : "y";
    var catAxis = horizontal ? "y" : "x";
    var scales = {};
    if (!circular) {
      scales[catAxis] = {
        display: !spark,
        stacked: stacked,
        grid: { display: false, drawTicks: false },
        border: { color: theme.axis },
        ticks: { color: theme.muted, font: { size: 12 }, maxRotation: 0, autoSkipPadding: 12, padding: 8 }
      };
      scales[valueAxis] = {
        display: !spark,
        stacked: stacked,
        beginAtZero: true,
        grid: { color: theme.grid, drawTicks: false },
        border: { display: false },
        ticks: {
          color: theme.muted,
          font: { size: 12 },
          padding: 8,
          maxTicksLimit: 6,
          callback: function (v) { return formatValue(v, format, true); }
        }
      };
    }

    var cfg = {
      type: type,
      data: { labels: (spec.data && spec.data.labels) || [], datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: reducedMotion() ? false : { duration: 350 },
        indexAxis: horizontal ? "y" : "x",
        layout: { padding: spark ? 2 : { top: 4, right: 4 } },
        interaction: circular ? { mode: "nearest", intersect: true } : { mode: "index", intersect: false },
        scales: scales,
        plugins: {
          legend: {
            display: showLegend,
            position: circular && canvas.clientWidth > 420 ? "right" : "bottom",
            align: circular ? "center" : "start",
            labels: {
              color: theme.muted,
              usePointStyle: true,
              pointStyle: type === "line" ? "line" : "rectRounded",
              boxWidth: 10,
              boxHeight: 10,
              padding: 14,
              font: { size: 12 }
            }
          },
          tooltip: {
            enabled: true,
            backgroundColor: theme.tooltipBg,
            titleColor: theme.tooltipText,
            bodyColor: theme.tooltipText,
            footerColor: theme.tooltipText,
            borderWidth: 0,
            padding: 10,
            cornerRadius: 8,
            boxWidth: 8,
            boxHeight: 8,
            boxPadding: 4,
            usePointStyle: true,
            titleFont: { weight: "600" },
            footerFont: { weight: "600" },
            callbacks: {
              label: function (ctx) {
                var v = circular ? ctx.parsed : (horizontal ? ctx.parsed.x : ctx.parsed.y);
                var name = circular ? ctx.label : ctx.dataset.label;
                var s = formatValue(v, format, false);
                if (circular) {
                  var total = ctx.dataset.data.reduce(function (a, b) { return a + (+b || 0); }, 0);
                  if (total) s += " · " + Math.round((v / total) * 100) + "%";
                }
                return name ? name + ": " + s : s;
              },
              footer: function (items) {
                if (!stacked || items.length < 2) return "";
                var sum = items.reduce(function (a, it) { return a + (horizontal ? it.parsed.x : it.parsed.y); }, 0);
                return "Total: " + formatValue(sum, format, false);
              }
            }
          }
        }
      },
      plugins: type === "line" && !spark ? [crosshairPlugin(theme.axis)] : []
    };
    if (!circular) cfg.options.plugins.legend.labels.generateLabels = legendLabels;
    if (isPlain(spec.options)) merge(cfg.options, spec.options);
    return cfg;
  }

  // Legend keys mirror the marks: a short line for line series (dashed when the
  // series is), a rounded square for bars. Chart.js would otherwise take the key
  // colour from the first point, whose border is the surface-coloured ring.
  function legendLabels(chart) {
    var items = window.Chart.defaults.plugins.legend.labels.generateLabels(chart);
    items.forEach(function (it) {
      var ds = chart.data.datasets[it.datasetIndex];
      if (!ds) return;
      var isLine = (ds.type || chart.config.type) === "line";
      var c = isLine ? ds.borderColor : (Array.isArray(ds.backgroundColor) ? ds.backgroundColor[0] : ds.backgroundColor);
      it.fillStyle = c;
      it.strokeStyle = c;
      it.lineWidth = isLine ? 2 : 0;
      it.lineDash = isLine && ds.borderDash ? ds.borderDash : [];
      it.pointStyle = isLine ? "line" : "rectRounded";
    });
    return items;
  }

  function renderChart(canvas) {
    var id = canvas.getAttribute("data-chart");
    var script = id ? doc.getElementById(id + "-config") : null;
    if (!script || !window.Chart || !charts) return;
    var text = script.textContent || "{}";
    var spec;
    try {
      spec = JSON.parse(text);
    } catch (e) {
      console.error("Chart '" + id + "': invalid JSON config", e);
      return;
    }
    var existing = charts.get(canvas);
    if (existing) {
      existing.destroy();
      charts.delete(canvas);
    }
    var theme = readTheme();
    window.Chart.defaults.font.family = theme.font;
    window.Chart.defaults.color = theme.muted;
    try {
      var chart = new window.Chart(canvas, buildConfig(spec, theme, canvas));
      chart._configText = text;
      charts.set(canvas, chart);
    } catch (e) {
      console.error("Chart '" + id + "' failed to render", e);
    }
  }

  // Draw charts that are new or whose config changed; destroy charts whose
  // canvas left the page. force=true redraws everything (theme change).
  function renderCharts(force) {
    if (!charts) return;
    charts.forEach(function (chart, canvas) {
      if (!canvas.isConnected) {
        chart.destroy();
        charts.delete(canvas);
      }
    });
    var canvases = doc.querySelectorAll("canvas[data-chart]");
    if (!canvases.length) return;
    ensureChartJs().then(function () {
      forEach(canvases, function (canvas) {
        if (!canvas.isConnected) return;
        var chart = charts.get(canvas);
        var script = doc.getElementById(canvas.getAttribute("data-chart") + "-config");
        var changed = chart && script && chart._configText !== script.textContent;
        if (force || !chart || changed) renderChart(canvas);
      });
    }).catch(function (e) { console.error(e); });
  }

  /* ---------------------------------------------------------------- tables */

  // A .table-wrap scrolls sideways when its table is too wide. When the table
  // fits, .is-fit turns the wrap back into a plain box so the sticky header
  // follows the page; otherwise .is-scrollable fades the edge(s) with more columns.
  var wraps = [];
  var sizeWatch = typeof ResizeObserver === "function" ? new ResizeObserver(function (entries) {
    entries.forEach(function (e) {
      var w = e.target.closest ? e.target.closest(".table-wrap") : null;
      if (w) updateWrap(w);
    });
  }) : null;

  function updateWrap(w) {
    if (!w.isConnected) return;
    var max = w.scrollWidth - w.clientWidth;
    var fits = max <= 1;
    w.classList.toggle("is-fit", fits);
    w.classList.toggle("is-scrollable", !fits);
    w.classList.toggle("is-scrolled", !fits && w.scrollLeft > 1);
    w.classList.toggle("is-scrolled-end", !fits && w.scrollLeft >= max - 1);
  }

  function onWrapScroll(evt) { updateWrap(evt.currentTarget); }

  function watchTables() {
    wraps = wraps.filter(function (w) {
      if (w.isConnected) return true;
      if (sizeWatch) {
        sizeWatch.unobserve(w);
        if (w._fitTable) sizeWatch.unobserve(w._fitTable);
      }
      return false;
    });
    forEach(doc.querySelectorAll(".table-wrap:not(.table-wrap--scroll)"), function (w) {
      if (!w._fitWatched) {
        w._fitWatched = true;
        wraps.push(w);
        w.addEventListener("scroll", onWrapScroll, { passive: true });
        if (sizeWatch) sizeWatch.observe(w);
      }
      var table = w.querySelector("table");
      if (sizeWatch && table !== w._fitTable) {
        if (w._fitTable) sizeWatch.unobserve(w._fitTable);
        if (table) sizeWatch.observe(table);
        w._fitTable = table;
      }
      updateWrap(w);
    });
  }

  /* ------------------------------------------------------------- swaps */

  // A swapped-in table row (hx-swap="outerHTML" on a <tr>) or any [data-flash]
  // element glows briefly, confirming the save. htmx removes .htmx-added within
  // ~20 ms, too soon for an animation, so the class is ours.
  function flashSwapped(el) {
    var targets = [];
    if (el.matches && el.matches("tr, [data-flash]")) targets.push(el);
    if (el.querySelectorAll) forEach(el.querySelectorAll("[data-flash]"), function (x) { targets.push(x); });
    targets.forEach(function (t) {
      t.classList.remove("flash-in");
      void t.offsetWidth; // restart the animation when the same element flashes again
      t.classList.add("flash-in");
      window.setTimeout(function () { t.classList.remove("flash-in"); }, 1800);
    });
  }

  // htmx fires afterSettle once per settled element; coalesce the page-wide work
  // into one pass. (setTimeout rather than requestAnimationFrame, which never
  // fires in hidden tabs.)
  var settleQueued = false;
  doc.addEventListener("htmx:afterSettle", function (evt) {
    var el = evt.target;
    if (el && el.nodeType === 1 && el.id !== "flash") flashSwapped(el);
    if (settleQueued) return;
    settleQueued = true;
    window.setTimeout(function () {
      settleQueued = false;
      openAutoDialogs();
      watchTables();
      renderCharts(false);
    }, 0);
  });

  if (window.matchMedia) {
    var scheme = window.matchMedia("(prefers-color-scheme: dark)");
    var onScheme = function () {
      window.setTimeout(function () { renderCharts(true); }, 0);
    };
    if (scheme.addEventListener) scheme.addEventListener("change", onScheme);
    else if (scheme.addListener) scheme.addListener(onScheme);
  }

  /* ------------------------------------------------------------------ init */

  function init() {
    watchFlash();
    openAutoDialogs();
    watchTables();
    renderCharts(false);
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
  else init();

  window.App = {
    toast: toast,
    renderCharts: function (force) { renderCharts(!!force); },
    formatMoney: function (v) { return formatValue(v, "usd", false); }
  };
})();
