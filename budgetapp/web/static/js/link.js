/* Plaid Link on the Connection page (ARCHITECTURE §7, §9; RESEARCH T2.2). Loaded only there.
 *
 * Buttons with data-link-mode="new" | "update" | "liabilities" (update modes also carry
 * data-item-id) open Plaid Link. The page asks the app for a Link token
 * (POST /connection/link-token → {link_token, attempt}), opens Link (OAuth opens Amex's own pop-up
 * on desktop: no redirect URI), then posts the result back and goes where the app says:
 *   a new link    POST /connection/exchange        {public_token, metadata, attempt}
 *   update mode   POST /connection/update-complete {item_id, mode, metadata}  (never exchanged)
 *   Link closed   POST /connection/link-exit       {event: "EXIT", attempt, ...}  (onExit)
 *   OAuth events  POST /connection/link-exit       {event: "CLOSE_OAUTH" | "FAIL_OAUTH", ...}
 * `attempt` ties the production link-in-progress marker to this Link session: only its own onExit
 * or exchange clears it. CLOSE_OAUTH / FAIL_OAUTH mean the Amex window closed or timed out while
 * Link itself stays open (it can still succeed), so they are only logged.
 *
 * Errors show inline in #link-error, in the page (a dialog closes first: nothing shows above a
 * modal). While Link runs, the clicked button and the button that opened its dialog both show the
 * busy state. Requests are same-origin JSON; the public token never goes into the URL, browser
 * storage or the console. The one exception to "no token in the page": an exchange whose access
 * token nothing could save answers with it once, and it is shown in #link-token-rescue to copy.
 *
 * The production confirm dialog ([data-link-checklist]) enables its Link button
 * ([data-needs-checklist]) only once every box is ticked.
 */
(function () {
  "use strict";

  var running = false;
  var busy = []; // {button, text}: buttons showing the busy state, with their own label

  function errorBox() {
    return document.getElementById("link-error");
  }

  function showError(text) {
    var box = errorBox();
    if (!box) {
      if (window.App && window.App.toast) window.App.toast("error", text);
      return;
    }
    var slot = box.querySelector("[data-link-error-text]");
    if (slot) slot.textContent = text;
    box.hidden = false;
    box.scrollIntoView({ block: "nearest" });
    box.focus({ preventScroll: true });
  }

  function clearError() {
    var box = errorBox();
    if (box) box.hidden = true;
  }

  /* The access token that nothing could save: shown once, kept until the page reloads. */
  function showToken(token) {
    var box = document.getElementById("link-token-rescue");
    var field = document.getElementById("link-token-rescue-value");
    if (!box || !field || typeof token !== "string" || !token) return;
    field.value = token;
    box.hidden = false;
    field.focus({ preventScroll: true });
    field.select();
  }

  function setBusy(buttons, text) {
    running = true;
    buttons.forEach(function (button) {
      if (!button || button.getAttribute("aria-busy") === "true") return;
      var label = button.querySelector("span");
      busy.push({ button: button, text: label ? label.textContent : null });
      button.setAttribute("aria-busy", "true");
      button.setAttribute("aria-disabled", "true");
      button.classList.add("is-busy");
    });
    relabel(text);
  }

  function relabel(text) {
    busy.forEach(function (entry) {
      var label = entry.button.querySelector("span");
      if (label) label.textContent = text;
    });
  }

  function clearBusy() {
    busy.forEach(function (entry) {
      var label = entry.button.querySelector("span");
      if (label && entry.text !== null) label.textContent = entry.text;
      entry.button.removeAttribute("aria-busy");
      entry.button.removeAttribute("aria-disabled");
      entry.button.classList.remove("is-busy");
    });
    busy = [];
    running = false;
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}),
    }).then(
      function (response) {
        if (response.status === 204) return {};
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (data) {
            if (!response.ok) {
              var error = new Error(
                data.error ||
                  "The app couldn't do that (error " + response.status + "). Reload the page and try again."
              );
              error.accessToken = data.access_token || null;
              throw error;
            }
            return data;
          });
      },
      function () {
        throw new Error("Can't reach the app. Check that it is still running, then reload the page.");
      }
    );
  }

  /* Only what the app uses from onSuccess's metadata (the server checks it again). */
  function slim(metadata) {
    metadata = metadata || {};
    var institution = metadata.institution || null;
    return {
      institution: institution
        ? { institution_id: institution.institution_id || null, name: institution.name || null }
        : null,
      accounts: (metadata.accounts || []).map(function (a) {
        return { id: a.id, name: a.name, mask: a.mask, type: a.type, subtype: a.subtype };
      }),
      link_session_id: metadata.link_session_id || null,
    };
  }

  function report(mode, itemId, attempt, eventName, error, metadata) {
    metadata = metadata || {};
    postJSON("/connection/link-exit", {
      mode: mode,
      item_id: itemId,
      attempt: attempt,
      event: eventName,
      status: metadata.status || null,
      link_session_id: metadata.link_session_id || null,
      error: error ? { error_code: error.error_code || null, error_type: error.error_type || null } : null,
    }).catch(function () {
      /* only a log line on the server; nothing to show */
    });
  }

  function go(data) {
    window.location.assign(data.redirect || "/connection");
  }

  function start(button) {
    var mode = button.getAttribute("data-link-mode");
    var rawId = button.getAttribute("data-item-id");
    var itemId = rawId ? parseInt(rawId, 10) : null;
    var dialog = button.closest("dialog");
    var opener = dialog && dialog.id ? document.querySelector('[data-dialog-open="' + dialog.id + '"]') : null;
    clearError();
    // Close a modal dialog first: Link opens above the page, and errors show in the page.
    if (dialog && dialog.open) dialog.close();
    if (!window.Plaid || typeof window.Plaid.create !== "function") {
      showError(
        "Plaid Link didn't load. Check the internet connection (and any content blocker for this page), then reload."
      );
      return;
    }
    setBusy([button, opener], "Opening Plaid…");
    postJSON("/connection/link-token", { mode: mode, item_id: itemId }).then(
      function (data) {
        var attempt = data.attempt || null;
        var finished = false;
        var handler = window.Plaid.create({
          token: data.link_token,
          onSuccess: function (publicToken, metadata) {
            finished = true;
            relabel(mode === "new" ? "Linking…" : "Finishing…");
            var request =
              mode === "new"
                ? postJSON("/connection/exchange", {
                    public_token: publicToken,
                    metadata: slim(metadata),
                    attempt: attempt,
                  })
                : postJSON("/connection/update-complete", {
                    item_id: itemId,
                    mode: mode,
                    metadata: slim(metadata),
                  });
            request.then(go, function (err) {
              showError(err.message);
              showToken(err.accessToken);
              clearBusy();
            });
          },
          onExit: function (error, metadata) {
            if (finished) return;
            finished = true;
            report(mode, itemId, attempt, "EXIT", error, metadata);
            if (error) {
              showError(
                error.display_message ||
                  error.error_message ||
                  "Plaid Link closed with an error. Nothing was changed."
              );
            }
            clearBusy();
            handler.destroy();
          },
          onEvent: function (eventName, metadata) {
            // The Amex window closed or timed out; Link stays open and can still succeed.
            if (eventName === "CLOSE_OAUTH" || eventName === "FAIL_OAUTH") {
              report(mode, itemId, attempt, eventName, null, metadata);
            }
          },
        });
        handler.open();
      },
      function (err) {
        showError(err.message);
        clearBusy();
      }
    );
  }

  // A dialog opener showing the busy state must not open its dialog again (app.js would).
  document.addEventListener(
    "click",
    function (event) {
      var opener = event.target.closest && event.target.closest('[data-dialog-open][aria-busy="true"]');
      if (!opener) return;
      event.preventDefault();
      event.stopPropagation();
    },
    true
  );

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-link-mode]");
    if (!button) return;
    event.preventDefault();
    if (running || button.disabled || button.getAttribute("aria-disabled") === "true") return;
    start(button);
  });

  document.addEventListener("change", function (event) {
    var list = event.target.closest("[data-link-checklist]");
    if (!list) return;
    var boxes = list.querySelectorAll('input[type="checkbox"]');
    var done = Array.prototype.every.call(boxes, function (box) {
      return box.checked;
    });
    var scope = list.closest("dialog") || document;
    scope.querySelectorAll("[data-needs-checklist]").forEach(function (button) {
      button.disabled = !done;
    });
  });
})();
