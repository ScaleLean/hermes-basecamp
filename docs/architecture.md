# Architecture

## Decision

Use the official Basecamp Python SDK as the runtime transport. Use the official
Basecamp CLI only for interactive onboarding and independent diagnostics.

This split gives the Hermes adapter native async I/O and direct access to SDK
retry, pagination, OAuth, error, observability, and webhook primitives. It also
keeps credential setup inspectable outside the gateway.

Primary sources:

- [Basecamp Python SDK](https://github.com/basecamp/basecamp-sdk/tree/main/python)
- [Basecamp OpenClaw channel](https://github.com/basecamp/openclaw-basecamp)
- [Hermes platform adapter guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/adding-platform-adapters.md)
- [Basecamp API](https://github.com/basecamp/bc-api)

## Identity boundary

One Hermes profile maps to one normal Basecamp member. The configuration must
contain an expected Basecamp account ID, per-account person ID, and member
email. The adapter calls `people.my_profile()` and compares all identity fields
before it connects. It repeats the check before every mutation.

There is no fallback credential. A missing, expired, or mismatched credential
stops the adapter.

## Runtime flow

```text
Basecamp project timeline
  -> SDK pagination and retry
  -> event ID replay guard
  -> suppress events authored by this member
  -> authenticated recording re-fetch
  -> direct mention or assignment gate
  -> Hermes session keyed by account and Basecamp target
  -> SDK write with this member's OAuth credential
  -> SDK read-back and creator-ID verification
```

Inbound delivery uses independent lanes. Tokenized webhooks cover supported
recording events. Campfire lines poll every 15 seconds,
the current member's notification inbox polls every 45 seconds, and project
timelines reconcile every five minutes. Campfire is not covered by Basecamp
webhooks, so the runtime keeps polling and reconciliation. Webhook payloads are
untrusted pointers. The receiver looks up the supplied event ID in the supplied
recording's canonical event history, then uses the recording type's exact SDK
getter. Actor, action, time, project, parent, content, assignments, and mentions
come only from those canonical responses. The durable queue leases one event at
a time and returns an interrupted lease to pending state.

Polling caps each Campfire discovery, Campfire line history, and per-project
timeline fetch at 500 items per cycle. Durable timestamp and event-ID cursors
remove overlap. Basecamp does not expose an after-cursor for these collections,
so a burst larger than the cap between polls can leave an older gap. Project
webhooks and the independent notification lane are the recovery paths.

## Configuration boundary

The plugin reads credentials through Hermes profile-scoped secret resolution.
Non-secret routing values are explicit:

- `BASECAMP_ACCOUNT_ID`
- `BASECAMP_PERSON_ID`
- `BASECAMP_PERSON_EMAIL`
- `BASECAMP_AGENT_MENTION`
- `BASECAMP_PROJECT_IDS`

OAuth values are secret:

- `BASECAMP_OAUTH_TOKEN_FILE` (preferred owner-only durable store)
- `BASECAMP_ACCESS_TOKEN`
- `BASECAMP_REFRESH_TOKEN`
- `BASECAMP_CLIENT_ID`
- `BASECAMP_CLIENT_SECRET`
- `BASECAMP_TOKEN_EXPIRES_AT`

The preferred path is an owner-only token file outside the repository. The
adapter persists refresh-token rotation atomically and echoes the stored
Basecamp 5 RFC 8707 resource indicator on refresh. Direct environment tokens
remain a short-lived compatibility fallback.

Onboarding supports both Basecamp authorization modes. Accounts that advertise
the Basecamp 5 authorization server use the public `basecamp-cli` device grant.
Accounts that advertise only Launchpad use a registered confidential client,
an exact localhost callback, CSRF state validation, and Launchpad's legacy token
format. Both paths verify the account, person ID, and email before saving a
token.

## Public-release gates

The current code is an alpha integration, not a 1.0 release. The remaining
release blockers are:

1. Add contract tests against recorded synthetic API fixtures and a live
   disposable Basecamp project.
2. Run cross-profile tests against separate live credentials to prove one
   agent cannot borrow another agent's
   credentials, projects, sessions, or outbound identity.
3. Complete an external security review for webhook forgery, prompt injection, replay,
   OAuth leakage, and bot-to-bot loops.
4. Prepare an upstream Hermes proposal only after the external plugin passes
   the reference canary and revocation test.

## Upstream path

Develop as a standalone plugin first. This keeps iteration independent of the
Hermes release cycle and creates a real adoption and reliability record. Once
the public-release gates pass, propose the same adapter boundary to Hermes as
a bundled platform. Do not fork the behavior into two implementations.
