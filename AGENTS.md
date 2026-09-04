# Agent instructions

Read `README.md` and `docs/project-brief.md` before changing this repository.

## Identity invariant

Every Hermes agent is a separate Basecamp member with a dedicated email
address and separate OAuth credentials. Never configure a Hermes agent with a
human member's Basecamp profile, token, session, or mailbox.

## Safety

- Keep credentials, OAuth tokens, refresh tokens, mailbox access, client data,
  and Basecamp exports out of Git.
- Require explicit approval before invitations, OAuth authorization, Basecamp
  writes, webhook creation, deployment, restart, or deletion.
- After each approved external action, read the result back from Basecamp.
- Preserve the existing local Hermes process and profile. Do not restart or
  reconfigure it unless the approved task requires that exact change.
- Use a dedicated Basecamp test project and synthetic content for the canary.

## Development

- Prefer the official Basecamp CLI, SDK, and API over custom endpoint wrappers.
- Keep identity selection explicit. Fail closed if the expected profile,
  member ID, account ID, or project ID does not match.
- Add focused tests for identity isolation, token isolation, retries,
  idempotency, and attribution read-back.
