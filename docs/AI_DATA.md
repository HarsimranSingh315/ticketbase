# External AI: what data leaves the system

Traced from the code (`app/supportrag.py` → `app/llm.py`), not assumed.

## When anything is sent
Only when **both** are true:
1. `LLM_API_KEY` is set in the environment. Unset (the default) means **no
   external AI at all** - suggestions still work using a local template.
2. An agent asks for a suggestion AND local retrieval found a confident
   knowledge-base match. If retrieval abstains, no request is made.

To switch external AI off immediately: remove `LLM_API_KEY` in Render and
redeploy. Ticket handling is unaffected (covered by tests).

## Exactly what is sent, per request
Destination: `LLM_API_BASE` (default `https://api.groq.com/openai/v1`), model
`LLM_MODEL`.

| Sent | Detail |
|---|---|
| System prompt | Fixed instructions (in `app/llm.py`) |
| The ticket's description | Free text, truncated to 2,000 characters |
| One KB article | The best-matching article's title and content, truncated to 4,000 characters |

**Important:** the description is whatever the customer wrote. If they typed a
name, phone number, address or account detail into it, that text goes to the
provider. TicketBase does not currently redact it.

## Never sent
Internal notes (enforced structurally and tested - `tests/test_notes.py`),
customer and contact records (names, emails, phone numbers stored in their own
fields), email drafts and sent messages, call records, other tickets, agent
names, and any credentials.

## Retention
Governed by the provider's terms for your account and plan, not by TicketBase.
Check them before enabling external AI for real customer data (HUMAN_TASKS H7).

## Safeguards (and their limits)
1. **Fencing:** ticket and article text go inside labelled blocks the model is
   told to treat as data; tag look-alikes are stripped so text can't fake a
   block boundary. *Limit: no prompt wording reliably stops injection.*
2. **Output validation:** a draft containing any link, email address or phone
   number that is not in the source article is discarded and the agent gets
   the template instead. *Limit: it can't catch every harmful instruction,
   e.g. a false promise with no contact detail in it.*
3. **Human approval:** nothing is ever sent to a customer without an agent
   reading and approving it. This is the backstop for what layers 1-2 miss.
4. **Size and time bounds:** inputs truncated as above; 500 output tokens;
   request timeout `LLM_TIMEOUT_SECONDS` (default 8s); any failure falls back
   to the template.

## Evidence
`tests/test_ai_safety.py` uses a mocked provider that deliberately returns
compromised output. It proves the safeguards catch it (mutation-checked:
disabling each safeguard makes its tests fail). It does **not** measure how
often a real model is fooled - that needs live model calls, an owner decision.
