"""
Seed knowledge base for SupportRAG (Project 2).

In a real system these would come from a company's actual help-center
articles / resolved-ticket history. Here they're hand-written so the
retrieval behaviour is easy to reason about and verify in tests - you
should be able to look at a ticket description, look at this list, and
predict which article it will match before running the code.

Each article has a `category` (the label SupportRAG will suggest) and a
`content` field (what gets cited/quoted in the drafted response).
"""

KB_ARTICLES = [
    {
        "title": "Wi-Fi keeps disconnecting",
        "category": "connectivity",
        "content": (
            "If Wi-Fi keeps dropping, first check whether other devices on the "
            "same network are also affected. Restart the router, forget and "
            "rejoin the network, and update the device's network drivers. "
            "Persistent drops usually indicate router firmware needing an "
            "update or too many devices on a 2.4GHz band."
        ),
    },
    {
        "title": "VPN will not connect",
        "category": "connectivity",
        "content": (
            "VPN connection failures are most often caused by an expired "
            "client certificate or a local firewall blocking the VPN port. "
            "Confirm the VPN client is on the latest version, restart it, "
            "and check whether the issue reproduces on a different network "
            "(e.g. mobile hotspot) to rule out local network blocking."
        ),
    },
    {
        "title": "Server outage / service down",
        "category": "connectivity",
        "content": (
            "When the server or service appears down for multiple users, "
            "check the status page first. If it confirms an outage, "
            "acknowledge the ticket, link the status page, and set an "
            "expectation for updates rather than attempting individual "
            "troubleshooting steps."
        ),
    },
    {
        "title": "Cannot log in / locked out of account",
        "category": "account_access",
        "content": (
            "Login failures are usually caused by an expired session, an "
            "incorrect password after a recent reset, or the account being "
            "temporarily locked after repeated failed attempts. Verify the "
            "account isn't locked, send a password reset link, and confirm "
            "two-factor authentication is set up correctly."
        ),
    },
    {
        "title": "Forgot password / reset not working",
        "category": "account_access",
        "content": (
            "If a password reset email never arrives, check spam/junk "
            "folders first, then confirm the email on file is correct and "
            "not typo'd. Reset links expire after a set window, so an "
            "old link failing is expected - send a fresh one."
        ),
    },
    {
        "title": "Two-factor authentication issues",
        "category": "account_access",
        "content": (
            "2FA problems are commonly caused by a new device, a changed "
            "phone number, or clock drift on the authenticator app. Offer "
            "backup codes if the user saved them during setup, and verify "
            "the user's identity through an alternate channel before "
            "manually resetting 2FA."
        ),
    },
    {
        "title": "Charged twice for the same order",
        "category": "billing",
        "content": (
            "Duplicate charges usually come from a failed payment retry "
            "that actually succeeded, or a double-click on checkout. "
            "Check the payment processor for two distinct transaction IDs "
            "on the same order. If confirmed duplicate, refund the extra "
            "charge and note the processor's dedupe window for the future."
        ),
    },
    {
        "title": "Unexpected charge on card",
        "category": "billing",
        "content": (
            "An unrecognized charge is often a subscription renewal the "
            "customer forgot about, or a charge under a slightly different "
            "billing descriptor. Pull the account's subscription and "
            "payment history before assuming it's fraudulent, and explain "
            "the charge clearly with the date and plan it corresponds to."
        ),
    },
    {
        "title": "Refund request",
        "category": "billing",
        "content": (
            "Refund requests should be checked against the refund policy "
            "window first. If eligible, process the refund and communicate "
            "the expected timeline for it to appear on the customer's "
            "statement, since that's the most common follow-up question."
        ),
    },
    {
        "title": "Monitor flickering or display issues",
        "category": "hardware",
        "content": (
            "Flickering displays are commonly a loose cable connection, an "
            "outdated graphics driver, or an incorrect refresh rate "
            "setting. Reseat the cable, update drivers, and check the "
            "display's refresh rate matches what the monitor supports."
        ),
    },
    {
        "title": "Mouse or keyboard not responding",
        "category": "hardware",
        "content": (
            "Unresponsive peripherals are usually a loose USB connection, "
            "a dead battery (for wireless devices), or a driver conflict "
            "after a recent OS update. Try a different USB port before "
            "assuming the hardware itself has failed."
        ),
    },
    {
        "title": "Printer not printing",
        "category": "hardware",
        "content": (
            "Printer issues are most often a stuck print queue, the "
            "printer being offline on the network, or low toner/ink not "
            "being detected correctly. Clear the print queue and confirm "
            "the printer shows as online before escalating to hardware "
            "replacement."
        ),
    },
    {
        "title": "Laptop battery draining fast or not charging",
        "category": "hardware",
        "content": (
            "A battery draining unusually fast is often a background "
            "app or browser tab pinning the CPU, a display brightness "
            "set too high, or a battery that has genuinely aged past "
            "its usable cycle count. A battery not charging at all is "
            "more likely a faulty cable, a dirty or damaged charging "
            "port, or a power adapter that doesn't match the device's "
            "wattage. Check battery health/cycle count in system "
            "settings, try a different cable and outlet, and only "
            "escalate to a hardware replacement once those are ruled out."
        ),
    },
    {
        "title": "Feature request: dark mode",
        "category": "feature_request",
        "content": (
            "Dark mode and similar UI preference requests are logged for "
            "the product team rather than resolved directly. Thank the "
            "customer for the suggestion, log it in the feature-request "
            "tracker, and let them know there's no committed timeline."
        ),
    },
    {
        "title": "Feature request: export to CSV",
        "category": "feature_request",
        "content": (
            "Export/import feature requests are common and should be "
            "logged with the specific use case the customer describes, "
            "since that context helps the product team prioritize. No "
            "workaround should be promised unless one is confirmed to "
            "exist."
        ),
    },
    {
        "title": "Suspicious login / possible account breach",
        "category": "security",
        "content": (
            "A reported suspicious login should be treated urgently: "
            "confirm recent login activity and IP/location with the "
            "customer, force a password reset, invalidate all active "
            "sessions, and recommend enabling two-factor authentication "
            "if it isn't already on."
        ),
    },
    {
        "title": "Data loss or missing data",
        "category": "security",
        "content": (
            "Reports of missing or lost data should be escalated quickly "
            "rather than troubleshot step-by-step at the front line - "
            "check backups and audit logs first to determine whether data "
            "was deleted, never synced, or is a display/filter issue "
            "rather than genuine loss."
        ),
    },
]
