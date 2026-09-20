// TicketBase — progressive enhancement only. Every page works with
// this file absent or failing: the "Suggest a category" form already
// works as a normal POST (see ticket_detail.html). This script's only
// job is to avoid a frozen-looking page while the LLM step (if
// enabled) is thinking, which can take a few seconds - a full-page
// reload with no feedback reads as broken.

document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("suggest-form");
    if (!form) return;

    var container = document.getElementById("suggestion-container");
    var ticketId = form.dataset.ticketId;

    form.addEventListener("submit", handleSubmit);

    function handleSubmit(event) {
        event.preventDefault();
        renderLoading();

        fetch("/tickets/" + ticketId + "/suggest", { method: "POST" })
            .then(function (res) {
                if (!res.ok) throw new Error("request failed: " + res.status);
                return res.json();
            })
            .then(renderSuggestion)
            .catch(function () {
                // Something about the AJAX path failed (offline, rate
                // limited, unexpected error) - fall back to a normal
                // full-page submit rather than leaving the loading
                // state stuck forever.
                form.removeEventListener("submit", handleSubmit);
                form.submit();
            });
    }

    function renderLoading() {
        container.innerHTML =
            '<div class="suggestion-box is-loading">' +
            '<span class="suggestion-label">Thinking…</span>' +
            '<p style="margin:0; font-size:13px; color: var(--ink-muted);">Checking the knowledge base…</p>' +
            "</div>";
    }

    function esc(value) {
        var div = document.createElement("div");
        div.textContent = value == null ? "" : value;
        return div.innerHTML;
    }

    function renderSuggestion(data) {
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
