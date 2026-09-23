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

    var button = form.querySelector("button");
    var container = document.getElementById("suggestion-container");
    var ticketId = form.dataset.ticketId;
    var errorBox = null;

    form.addEventListener("submit", handleSubmit);

    function handleSubmit(event) {
        event.preventDefault();
        setLoading(true);
        clearError();

        // credentials: "same-origin" made explicit, not relied on as a
        // browser default - this endpoint now requires a real session
        // (see require_session_or_api_key in app/auth.py), so the
        // session cookie must actually be sent with this request.
        fetch("/tickets/" + ticketId + "/suggest", { method: "POST", credentials: "same-origin" })
            .then(function (res) {
                if (res.status === 401) throw new HttpError(401, "Your session expired - reload the page and log in again.");
                if (res.status === 429) throw new HttpError(429, "You're doing that a bit fast — wait a moment and try again.");
                if (!res.ok) throw new HttpError(res.status, "Something went wrong on the server (status " + res.status + "). Try again.");
                return res.json();
            })
            .then(function (data) {
                setLoading(false);
                renderSuggestion(data);
            })
            .catch(function (err) {
                setLoading(false);
                var message = err instanceof HttpError
                    ? err.message
                    : "Couldn't reach the server — check your connection and try again.";
                showError(message);
            });
    }

    function HttpError(status, message) {
        this.status = status;
        this.message = message;
    }
    HttpError.prototype = Object.create(Error.prototype);

    function setLoading(isLoading) {
        if (!button) return;
        button.disabled = isLoading;
        button.textContent = isLoading ? "Thinking…" : "Suggest a category";
    }

    function showError(message) {
        clearError();
        errorBox = document.createElement("p");
        errorBox.className = "form-error-inline";
        errorBox.setAttribute("role", "alert");
        errorBox.textContent = message;
        form.insertAdjacentElement("afterend", errorBox);
    }

    function clearError() {
        if (errorBox && errorBox.parentNode) errorBox.parentNode.removeChild(errorBox);
        errorBox = null;
    }

    function esc(value) {
        var div = document.createElement("div");
        div.textContent = value == null ? "" : value;
        return div.innerHTML;
    }

    function renderSuggestion(data) {
        // Only replace the container's content once we have a real,
        // final result - this is the point where the form is meant to
        // go away, having done its job.
        if (data.abstained) {
            container.innerHTML =
                '<div class="suggestion-box is-abstained">' +
                '<span class="suggestion-label">Not confident enough</span>' +
                '<p style="margin:0; font-size:13px; color: var(--ink-muted);">' + esc(data.draft_response) + "</p>" +
                sourcesHTML(data.sources) +
                "</div>";
            return;
        }

        var pct = Math.round(data.confidence * 100);
        var sourceLabel = data.draft_source === "llm"
            ? "(AI-written, grounded in the source above)"
            : "(templated from the source above)";

        container.innerHTML =
            '<div class="suggestion-box">' +
            '<span class="suggestion-label">Suggested, not yet confirmed</span>' +
            '<p class="suggested-category">' + esc(data.category) + "</p>" +
            '<div class="confidence-row">' +
            '<div class="confidence-track"><div class="confidence-fill" style="width:' + pct + '%;"></div></div>' +
            '<span class="confidence-value">' + pct + '%</span>' +
            "</div>" +
            sourcesHTML(data.sources) +
            '<details class="disclosure"><summary>Drafted response ' + sourceLabel + "</summary>" +
            '<pre class="draft-response">' + esc(data.draft_response) + "</pre></details>" +
            "</div>" +
            '<p class="hint">This is a suggestion, not a decision. Use "Confirm category" above to accept it.</p>';
    }

    function sourcesHTML(sources) {
        if (!sources || !sources.length) return "";
        var items = sources.map(function (s) {
            return "<li>" + esc(s.title) + ' <span class="sim">(' + Math.round(s.similarity * 100) + "% similar)</span></li>";
        }).join("");
        return '<details class="disclosure"><summary>What this is based on</summary><ul class="sources-list">' + items + "</ul></details>";
    }
});
