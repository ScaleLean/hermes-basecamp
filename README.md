# Hermes Basecamp

Hermes Basecamp makes one Hermes agent a dedicated Basecamp teammate.

The agent uses its own Basecamp member, email address, OAuth grant, Hermes home,
gateway, memory, and local state. It never acts through a human member's token.
Its replies and work appear under its own Basecamp identity.

This standalone Hermes platform plugin uses Basecamp's official Python SDK. Its
interaction model follows Basecamp's
[official OpenClaw plugin](https://github.com/basecamp/openclaw-basecamp) and
[agent model](https://basecamp.com/agents).

**Status:** 0.3 development. The live Maximus member has passed one real
Campfire mention and reply test. The 24-hour 1.0 release gate has not passed.

## Participation model

Hermes starts a full turn for:

- a Ping from a non-client teammate;
- a structured mention in an allowlisted project;
- a to-do, card, or card-step assigned to the agent;
- a follow-up on a recording where the agent is active;
- a check-in prompt addressed to the agent.

Other project activity is quiet context. Agent-authored events are ignored.
Ping replies return to the same Circle. Campfire replies return to the same
Campfire. Recording work uses comments. Check-ins use answers. A successful
assigned to-do receives a verified result comment before it is completed. A
failed task stays open.

Webhook and polling input share one SQLite inbox. Cursor advancement and event
acceptance are one transaction. Assignment snapshots use durable generations,
so an agent comment cannot retrigger the same active assignment. Outbound reply
intents are also durable. After an uncertain network result, the plugin searches
for canonical agent-authored proof before it retries only the saved write. It
does not run Hermes again.

## Public interface

Targets use Basecamp's grammar:

```text
recording:<id>
bucket:<id>
bucket:<bucket_id>/recording:<recording_id>
ping:<circle_id>
```

The plugin exposes ten tools:

- `basecamp_create_todo`
- `basecamp_complete_todo`
- `basecamp_reopen_todo`
- `basecamp_read_history`
- `basecamp_add_boost`
- `basecamp_move_card`
- `basecamp_post_message`
- `basecamp_answer_checkin`
- `basecamp_api_read`
- `basecamp_api_write`

The generic tools use a generated, frozen inventory of official SDK routes.
They still call typed SDK services. They require an explicit project scope and
reject foreign hosts, query strings in paths, denied projects, unknown routes,
Adminland operations, and ownership that Basecamp cannot prove.

The policy registry classifies 195 SDK operations. It exposes 189 project-member
operations through focused or generic tools and denies six Adminland operations.
The full 274-method SDK inventory also records internal and intentionally
excluded methods. CI fails on any unclassified SDK change.

## Install

Requirements are Hermes Agent and Python 3.11 through 3.13.

```sh
hermes plugins install ScaleLean/hermes-basecamp --enable
python -m pip install 'basecamp-sdk>=0.16.0,<0.17' 'markdown-it-py>=3,<5'
```

Use one configuration per agent:

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
      token_file: /absolute/path/outside-the-repository/agent-oauth.json
      webhook_public_url: https://agent.example.com/basecamp/webhooks/SECRET
```

Keep OAuth credentials, webhook secrets, and live identifiers outside Git. See
[`.env.example`](.env.example) for every setting.

## Operator commands

```sh
hermes-basecamp setup --public-url https://agent.example.com/basecamp/webhooks/SECRET --yes
hermes-basecamp doctor --probe
hermes-basecamp webhooks sync --yes
hermes-basecamp journal list
hermes-basecamp journal reconcile --help
hermes-basecamp test live --campfire-id ID --todolist-id ID --yes
```

`test live` requires separate conductor credentials. It does not use the
agent's OAuth grant to create its own test event. Invitations, access changes,
archive, trash, and deletion are never part of that suite.

The receiver binds to loopback. Production uses a stable HTTPS proxy or tunnel.
Polling remains active when webhooks are available.

## Health states

- `starting`: identity or initial lane probes are incomplete.
- `ready`: identity, role, every required receive lane, inbox, and webhooks are healthy.
- `recovering`: one lane or webhook path is degraded while safety-net ingestion continues.
- `blocked`: identity, OAuth, membership, or configuration prevents operation.
- `stopped`: the gateway was intentionally disconnected.

`doctor --probe` reports each lane's last success, cursor age, queue depth,
oldest pending age, poison count, last completed Hermes run, and webhook state.
Authentication alone never reports `ready`.

## Development

```sh
python -m pytest -p no:cacheprovider -q
ruff check .
mypy --explicit-package-bases .
python scripts/generate_sdk_routes.py --check
python scripts/scan_secrets.py
python scripts/scan_secrets.py --history
python -m build
hermes plugins doctor . --ci
```

CI tests Python 3.11, 3.12, and 3.13 against the minimum pinned Hermes revision
and current Hermes main. See [the architecture](docs/architecture.md),
[capability policy](docs/capabilities.md), and [1.0 release gate](docs/release-gate.md).

## License

MIT. See [`LICENSE`](LICENSE).
