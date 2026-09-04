# Hermes Basecamp

Bring a Hermes agent into Basecamp as a teammate, not as your shadow.

Each agent signs in with its own Basecamp member account. It has its own name,
email address, project access, and activity history. Add it to a project,
mention it, or assign it work. Its replies and changes appear under its own
identity.

Hermes Basecamp is a native Hermes platform plugin built on the official
Basecamp Python SDK and Basecamp's [agent-first model](https://basecamp.com/agents).

**Status:** Public alpha. The identity and safety contracts are tested; the
public interface can still change before 1.0.

## What it does

- Listens for Campfire messages, assignments, notifications, and project
  activity.
- Replies in the same Campfire or Basecamp recording.
- Reads and works with projects, people, to-dos, cards, messages, schedules,
  check-ins, documents, files, search, reports, timesheets, and more.
- Sends and receives text and attachments.
- Uses webhooks when available and bounded polling as the safety net.

The plugin classifies 195 operations behind eight domain tools. Of the 274
public methods in `basecamp-sdk` 0.16, 189 are available to agents, six support
the runtime internally, and 79 are intentionally excluded. The inventory test
fails when an SDK update introduces an unclassified method.

See [`docs/capabilities.md`](docs/capabilities.md) for the complete policy.

## The member is the permission model

The agent can only do what its Basecamp member can do in projects you have
explicitly allowed. Before work begins, the plugin verifies the configured
account, person ID, email address, employee role, and project memberships.
Every resource is then checked against Basecamp before it is read or changed.

Ordinary changes require a direct Basecamp interaction or an approved
schedule. Destructive, access-changing, archival, and cross-project actions
require a fresh approval for that exact action. Unknown operations fail
closed. A timed-out change is recorded as uncertain and is never retried
blindly.

The plugin does not automate Basecamp Adminland. Creating the member, accepting
an invitation, billing, ownership, account security, and employee or client
classification remain human work.

## Install

You need Hermes Agent, Python 3.11 through 3.13, and one dedicated Basecamp
member for each Hermes agent.

Install the plugin:

```sh
hermes plugins install ScaleLean/hermes-basecamp --enable
```

Hermes reports the two declared Python dependencies during installation.
Install them in the same Python environment that runs Hermes if needed:

```sh
python -m pip install 'basecamp-sdk>=0.16.0,<0.17' 'markdown-it-py>=3,<5'
```

Configure the member and the projects it may use:

```yaml
gateway:
  platforms:
    basecamp:
      enabled: true
      account_id: "1234567"
      person_id: "7654321"
      person_email: agent@example.com
      mention: "@HermesAgent"
      project_ids: ["11111111"]
      token_file: /absolute/path/outside/the-repository/agent-oauth.json
```

Keep OAuth credentials and `BASECAMP_WEBHOOK_TOKEN` in the profile-scoped
secret environment, not in YAML or this repository. Environment-based
configuration is documented in [`.env.example`](.env.example).

Verify the installation, then restart Hermes:

```sh
hermes plugins doctor ~/.hermes/plugins/basecamp-platform --ci
hermes gateway restart
```

## Authorize the member

Use a separate OAuth grant for every agent. Never reuse a human member's token.

Basecamp accounts that support device authorization can use:

```sh
python scripts/basecamp_oauth.py \
  --account-id ACCOUNT_ID \
  --person-id PERSON_ID \
  --email AGENT_EMAIL \
  --output /path/outside-repository/agent-oauth.json
```

Accounts that use Launchpad require a registered OAuth application:

```sh
python scripts/basecamp_launchpad_oauth.py \
  --app-credentials /path/outside-repository/basecamp-app.json \
  --account-id ACCOUNT_ID \
  --person-id PERSON_ID \
  --email AGENT_EMAIL \
  --output /path/outside-repository/agent-oauth.json
```

Both flows verify the selected account, member, and email before writing an
owner-only token file. Run the read-only identity check at any time with:

```sh
python scripts/basecamp_doctor.py
```

## Webhooks

Polling works without a public endpoint. Webhooks make delivery faster.

The built-in receiver binds to loopback by default. Expose it only through a
trusted HTTPS reverse proxy. A non-loopback bind is rejected unless
`BASECAMP_WEBHOOK_TLS_PROXY=true` records that protection explicitly.

## Recovery

Every change has an idempotency key and a durable journal. Inspect unresolved
work after an interruption:

```sh
hermes-basecamp journal-list --profile-id ACCOUNT_ID:PERSON_ID
```

A pre-dispatch reservation can be released explicitly. A dispatched or
uncertain change first requires a Basecamp reconciliation that proves the
change did not happen. See [`docs/capabilities.md`](docs/capabilities.md) for
the exact recovery contract.

## Development

```sh
python -m pytest -q
ruff check .
mypy --explicit-package-bases .
python -m build
hermes plugins doctor . --ci
```

The supported Python versions run in CI. Live tests are opt-in and require a
dedicated Basecamp test member. The project remains an alpha until its public
interfaces and field behavior have earned a stable 1.0 promise.

## License

MIT. See [`LICENSE`](LICENSE).
