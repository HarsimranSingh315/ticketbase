# Browser checks

Real headless-Chromium checks for behaviour unit tests can't see (layout,
JS flows, focus). Run against a local dev server:

    DATABASE_URL=sqlite:///./browser.db SECRET_KEY=$(python -c 'print("b"*48)') \
    BOOTSTRAP_ADMIN_EMAIL=admin@browser.test BOOTSTRAP_ADMIN_PASSWORD=browser-pass-123 \
    uvicorn app.main:app --port 8811 &
    curl -X POST localhost:8811/tickets -H 'content-type: application/json' -d '{"description":"VPN will not connect"}'
    node tests/browser/suggestion_flow.js
    node tests/browser/mobile_overflow.js     # every page must print "ok" at 390px

Requires Node Playwright and a Chromium build (set executablePath in each
script to your local Chromium).
