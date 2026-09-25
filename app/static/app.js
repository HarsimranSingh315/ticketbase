// TicketBase — progressive enhancement only. Every page works with
// this file absent or failing: the "Suggest a category" form already
// works as a normal POST (see ticket_detail.html). This script's job
// is to avoid a frozen-looking page while the LLM step (if enabled)
// is thinking, which can take a few seconds - a full-page reload with
// no feedback reads as broken - AND to survive failure visibly.
//
// Bug fixed here (found via code review, not just assumed): an earlier
// version replaced the whole #suggestion-container's innerHTML on
// submit, which DESTROYED the #suggest-form element living inside it
// (it's a child of that container). The failure path then tried
// form.submit() on a form no longer attached to the document, which is
// unreliable across browsers, and there was no visible error state at
// all - a failed request just left "Thinking..." on screen forever.
// Fixed by never touching the form itself until we have a final
// result: only the submit button's state changes while a request is
// in flight, and failures render a real, specific error message next
// to the still-intact, still-submittable form instead of guessing at
// a fallback submit.

document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("suggest-form");
    if (!form) return;

    // B12: results render into their OWN region beside the form. The old
    // code replaced the whole container, deleting the form - there was no
    // way to retry or generate again, and a hung request left the button on
    // "Thinking..." forever (no timeout).
    var TIMEOUT_MS = 30000;
    var button = form.querySelector("button");
    var idleLabel = button ? button.textContent : "Suggest a category";
    var ticketId = form.dataset.ticketId;
    var inFlight = null;

    var result = document.createElement("div");
    result.id = "suggestion-result";
    result.setAttribute("aria-live", "polite");
    form.insertAdjacentElement("afterend", result);

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (inFlight) return;  // no double submissions
        request();
    });

    function request() {
        var controller = typeof AbortController === "function" ? new AbortController() : null;
        var timer = controller ? setTimeout(function () { controller.abort(); }, TIMEOUT_MS) : null;
        inFlight = controller || true;
        setPending(true);

        var csrfField = form.querySelector('input[name="csrf_token"]');
        fetch("/tickets/" + ticketId + "/suggest", {
            method: "POST",
            credentials: "same-origin",
            headers: { "X-CSRF-Token": csrfField ? csrfField.value : "" },
            signal: controller ? controller.signal : undefined
        })
            .then(function (res) {
                if (res.status === 401) throw new UserFacingError("Your session expired. Reload the page and sign in again.");
                if (res.status === 403) throw new UserFacingError("This page is out of date. Reload it and try again.");
                if (res.status === 429) throw new UserFacingError("Too many requests just now. Wait a moment, then try again.");
                if (!res.ok) throw new UserFacingError("The server couldn't produce a suggestion (error " + res.status + ").");
                return res.json();
            })
            .then(function (data) { renderSuggestion(data); })
            .catch(function (err) {
                var message;
                if (err && err.name === "AbortError") message = "That took too long, so it was stopped. You can try again.";
                else if (err instanceof UserFacingError) message = err.message;
                else message = "Couldn't reach the server. Check your connection and try again.";
                renderError(message);
            })
            .then(function () {  // always runs: clean up
                if (timer) clearTimeout(timer);
                inFlight = null;
                setPending(false);
            });
    }

    function UserFacingError(message) { this.message = message; }
    UserFacingError.prototype = Object.create(Error.prototype);

    function setPending(pending) {
        result.setAttribute("aria-busy", pending ? "true" : "false");
        if (!button) return;
        button.disabled = pending;
        button.textContent = pending ? "Working on a suggestion…" : (result.dataset.hasResult ? "Suggest again" : idleLabel);
        if (pending) {
            result.innerHTML = '<div class="suggestion-box is-pending"><span class="skeleton-line"></span>' +
                '<span class="skeleton-line short"></span><span class="visually-hidden">Working on a suggestion</span></div>';
        }
    }

    function renderError(message) {
        result.dataset.hasResult = "";
        result.innerHTML = '<p class="form-error-inline" role="alert">' + esc(message) + "</p>";
    }

    function esc(value) {
        var div = document.createElement("div");
        div.textContent = value == null ? "" : String(value);
        return div.innerHTML;
    }

    function score(value) {
        var n = Number(value);
        return isFinite(n) ? n.toFixed(2) : "–";
    }

    function renderSuggestion(data) {
        result.dataset.hasResult = "1";
        if (data.abstained) {
            result.innerHTML =
                '<div class="suggestion-box is-abstained">' +
                '<span class="suggestion-label">No confident match</span>' +
                "<p>" + esc(data.draft_response) + "</p>" +
                sourcesHTML(data.sources) + "</div>";
            return;
        }
        var sourceLabel = data.draft_source === "llm"
            ? "AI-written from the article(s) below - check it before use"
            : "Template built from the article(s) below";
        result.innerHTML =
            '<div class="suggestion-box">' +
            '<span class="suggestion-label">Suggested - not applied</span>' +
            '<p class="suggested-category">' + esc(data.category) + "</p>" +
            '<p class="hint">Match score ' + score(data.confidence) +
            " (text similarity to the knowledge base, 0 to 1 - not a probability)</p>" +
            sourcesHTML(data.sources) +
            '<details class="disclosure"><summary>Draft reply (' + sourceLabel + ")</summary>" +
            '<pre class="draft-response">' + esc(data.draft_response) + "</pre></details>" +
            "</div>" +
            '<p class="hint">Nothing changes until you use "Confirm category" above.</p>';
    }

    function sourcesHTML(sources) {
        if (!sources || !sources.length) return "";
        var items = sources.map(function (s) {
            var id = parseInt(s.article_id, 10);
            var title = esc(s.title);
            var link = isFinite(id) ? '<a href="/kb/' + id + '">' + title + "</a>" : title;
            return "<li>" + link + ' <span class="sim">score ' + score(s.similarity) + "</span></li>";
        }).join("");
        return '<details class="disclosure"><summary>Based on these articles</summary><ul class="sources-list">' + items + "</ul></details>";
    }
});
