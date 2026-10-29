/* Plaid Link on the Connection page (ARCHITECTURE §7, §9; RESEARCH T2.2). Loaded only there.
 *
 * Buttons with data-link-mode="new" | "update" | "liabilities" (update modes also carry
 * data-item-id) open Plaid Link. The page asks the app for a Link token
 * (POST /connection/link-token), opens Link (OAuth opens Amex's own pop-up on desktop: no
 * redirect URI), then posts the result back and goes where the app says:
 *   a new link    POST /connection/exchange        {public_token, metadata}
 *   update mode   POST /connection/update-complete {item_id, mode, metadata}  (never exchanged)
 *   not linked    POST /connection/link-exit       (onExit, and the CLOSE_OAUTH / FAIL_OAUTH events)
 * Errors show inline in #link-error. Requests are same-origin JSON; the public token never
 * goes into the URL, browser storage or the console.
 *
 * The production confirm dialog ([data-link-checklist]) enables its Link button
 * ([data-needs-checklist]) only once every box is ticked.
 */
(function () {
  "use strict";

  var running = false;

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

  function setBusy(button, on) {
    running = on;
    var label = button.querySelector("span");
    if (on) {
      if (label) {
        button.setAttribute("data-label", label.textContent);
        label.textContent = "Opening Plaid…";
      }
      button.setAttribute("aria-busy", "true");
      button.classList.add("is-busy");
    } else {
      if (label && button.hasAttribute("data-label")) {
        label.textContent = button.getAttribute("data-label");
        button.removeAttribute("data-label");
      }
      button.removeAttribute("aria-busy");
      button.classList.remove("is-busy");
    }
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
              throw new Error(
                data.error ||
                  "The app couldn't do that (error " + response.status + "). Reload the page and try again."
              );
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

  function reportExit(mode, itemId, error, metadata, eventName) {
    metadata = metadata || {};
    postJSON("/connection/link-exit", {
      mode: mode,
      item_id: itemId,
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
    clearError();
    if (!window.Plaid || typeof window.Plaid.create !== "function") {
      showError(
        "Plaid Link didn't load. Check the internet connection (and any content blocker for this page), then reload."
      );
      return;
    }
    var dialog = button.closest("dialog");
    if (dialog && dialog.open) dialog.close(); // Link must open above the page, not under a modal
    setBusy(button, true);
    postJSON("/connection/link-token", { mode: mode, item_id: itemId }).then(
      function (data) {
        var finished = false;
        var handler = window.Plaid.create({
          token: data.link_token,
          onSuccess: function (publicToken, metadata) {
            finished = true;
            var request =
              mode === "new"
                ? postJSON("/connection/exchange", { public_token: publicToken, metadata: slim(metadata) })
                : postJSON("/connection/update-complete", {
                    item_id: itemId,
                    mode: mode,
                    metadata: slim(metadata),
                  });
            request.then(go, function (err) {
              showError(err.message);
              setBusy(button, false);
            });
          },
          onExit: function (error, metadata) {
            if (finished) return;
            finished = true;
            reportExit(mode, itemId, error, metadata, "EXIT");
            if (error) {
              showError(
                error.display_message ||
                  error.error_message ||
                  "Plaid Link closed with an error. Nothing was changed."
              );
            }
            setBusy(button, false);
            handler.destroy();
          },
          onEvent: function (eventName, metadata) {
            if (eventName === "CLOSE_OAUTH" || eventName === "FAIL_OAUTH") {
              reportExit(mode, itemId, null, metadata, eventName);
            }
          },
        });
        handler.open();
      },
      function (err) {
        showError(err.message);
        setBusy(button, false);
      }
    );
  }

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
