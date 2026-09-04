# Hermes Agent to Basecamp native integration

Date: 2026-09-03

## Direct answer

The requested identity model is supported today, but only by treating each
Hermes agent as a normal Basecamp user. Give each agent a unique mailbox. Have
a Basecamp administrator invite it as **Someone who works at your company**.
Accept the invitation for that identity. Then authorize a separate Basecamp
OAuth or CLI profile while signed in as that agent. Calls made with that token
are attributed to the agent's user, not to the human operator. Basecamp's own
experimental local connector uses this exact pattern: a real Basecamp agent
user, a matching CLI profile, and a startup check that the profile resolves to
the agent rather than the operator. [Basecamp Agent Connector
README](https://github.com/basecamp/basecamp-local-agent-connector/blob/main/README.md)

Do not use Basecamp's delegated `Agent` attribution for this requirement.
Basecamp now documents events where `creator` remains the human user and
`performed_by` names an `Agent`. That is an audit marker for an agent acting on
someone's behalf. It is not an independent full-member identity.
[Delegated events](https://github.com/basecamp/bc-api/blob/master/sections/events.md#delegated-events)

Basecamp has also published a proposal for a future first-class `Agent`
personable type. The proposal explicitly says that this type would not be a
seat, would not have a Launchpad login, and would authenticate with an agent
token. It therefore would not satisfy the requirement that every agent have
its own email address and be a full member. It is also not a stable public API
contract today. The public event documentation was shipped before agent
availability, and Basecamp's pricing page still labels its official AI
connectors and full MCP support as coming soon. [Built-in agent support
proposal](https://github.com/basecamp/basecamp-local-agent-connector/blob/main/docs/built-in-support.md),
[delegated-events documentation pull request](https://github.com/basecamp/bc-api/pull/436),
and [Basecamp pricing](https://basecamp.com/pricing)

Recommendation: build a standalone Hermes platform plugin around one normal
Basecamp member account per Hermes agent. Use the official Basecamp SDK for the
runtime and the official CLI for setup and diagnostics. Use the official
OpenClaw Basecamp channel as the closest public reference architecture. Do not
derive code or behavior from the previous internal implementation.

## Identity decision

| Basecamp identity model | Independent author | Own email and login | Full member | Fit |
| --- | --- | --- | --- | --- |
| Normal Basecamp user invited to the primary company | Yes | Yes | Yes | **Use this now** |
| Delegated `Agent` in `performed_by` | No. `creator` remains the authorizing user. | Not established by the public API | No | Reject |
| Proposed first-class `Agent` personable type | Proposed to author its own actions | Proposal says no Launchpad identity | Proposal says not a seat | Reject for this requirement |
| Basecamp chatbot/integration | Bot-style chat author | No member login | No | Reject |

Basecamp defines a person who works at the account's primary company as a full
team member. This user type can create projects, add existing users to projects,
and be made an administrator or owner. An outside collaborator can work in
assigned projects but cannot create projects, invite people, add people, or
become an administrator. [Basecamp 5 permissions](https://5.basecamp-help.com/article/1171-permissions)

This creates a material least-privilege limitation. A full member receives
people and project management abilities that an integration-only identity does
not need. Keep every agent as a non-admin and non-owner. Restrict the projects
it can see. Also restrict the commands exposed by the Hermes plugin. These
application controls reduce accidental use, but they do not reduce the native
permissions of the underlying Basecamp member. [Basecamp 5 permissions](https://5.basecamp-help.com/article/1171-permissions)

Basecamp chatbots are a separate mechanism. They use chatbot-specific keys,
not user OAuth, and posting chatbots have a blind write-only URL. They cannot
read the chat room. An interactive chatbot receives a command and returns a
chat response, but it is still an integration identity rather than a member.
[Basecamp chatbot API](https://github.com/basecamp/bc-api/blob/master/sections/chatbots.md)

## Member creation and required administrator work

The exact full-member onboarding path is administrative and interactive:

1. Provision a unique, monitored mailbox for the agent. It must be able to
   receive the Basecamp invitation, login links, and recovery messages.
2. A Basecamp administrator uses **Invite people to the account** and selects
   **Someone who works at your company**. Only administrators can invite new
   users on current accounts. Basecamp sends the invitation to the supplied
   email address. [Inviting people](https://5.basecamp-help.com/article/1060-inviting-people)
3. Accept the emailed invitation and create the agent's Basecamp login. New
   users cannot log in before accepting the invitation. After acceptance, an
   administrator cannot change that user's name or email; the user must change
   them. [Managing people](https://5.basecamp-help.com/article/1185-managing-people)
4. Add only the required projects. People can see only projects to which they
   have been added. [Inviting people](https://5.basecamp-help.com/article/1060-inviting-people)
5. While signed in as that agent, complete OAuth authorization for a dedicated
   CLI profile or plugin credential. Basecamp OAuth asks the signed-in user to
   authorize the application. Access tokens have a two-week lifetime and can
   be renewed with a refresh token. [Basecamp authentication](https://github.com/basecamp/bc-api/blob/master/sections/authentication.md)
6. Verify the identity before enabling writes. The official connector uses
   `basecamp auth login --profile <agent>` followed by `basecamp me --profile
   <agent>`. The official CLI states that actions are posted as the
   authenticated user for the selected profile. [Basecamp CLI profiles](https://github.com/basecamp/basecamp-cli#multiple-profiles)

The public People API does not provide a supported one-call replacement for
these steps. It has a project-scoped `PUT /projects/{id}/people/users.json`
operation. That operation can grant or revoke existing people and can create a
new person from a name and email address, with optional title and company name.
It has no documented parameter for the account-wide user type or the
`employee` flag. Its own example returns the newly created person with
`employee: false`. [People API](https://github.com/basecamp/bc-api/blob/master/sections/people.md#update-who-can-access-a-project)

Therefore, the API can create a person in the course of granting project
access, but the public contract does not guarantee that it creates a full team
member. Adding an unknown email from a project in the Basecamp UI also defaults
to Outside Collaborator and offers a later conversion step. Use the account
invite UI for deterministic full-member creation, or create the person through
the API and require an administrator to change its company/type in Adminland.
[Adding people to a project](https://5.basecamp-help.com/article/1073-adding-people-to-a-project)
and [managing people](https://5.basecamp-help.com/article/1185-managing-people)

## Official building blocks

### Basecamp CLI and MCP server

The official `basecamp` CLI supports normal commands, structured JSON for
agents, named OAuth profiles, and a stdio MCP server. The MCP server exposes 15
domain tools and can be restricted to read-only mode or selected domains. A
profile stores its own OAuth credentials; `--profile` or `BASECAMP_PROFILE`
selects which authenticated user performs a command. [Basecamp CLI](https://github.com/basecamp/basecamp-cli)
and [Basecamp agent skill](https://github.com/basecamp/basecamp-cli/blob/main/skills/basecamp/SKILL.md)

This is the fastest way to prove outbound Basecamp access from Hermes. Hermes
supports stdio and HTTP MCP servers, and its official platform-plugin guide
also permits a plugin to register platform-specific tools. [Hermes native MCP
reference](https://github.com/NousResearch/hermes-agent/blob/main/skills/autonomous-ai-agents/hermes-agent/references/native-mcp.md)
and [Hermes platform adapter guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/adding-platform-adapters.md)

MCP alone is not a native Basecamp conversation channel. It provides tools
when Hermes is already running. It does not provide durable inbound mentions,
assignments, chat lines, or thread continuation. The native integration still
needs a Hermes platform adapter and an inbound event strategy.

### Basecamp SDK and REST API

Basecamp publishes an official multi-language SDK and an OpenAPI 3.1
description. The repository contains Go, Ruby, TypeScript, Swift, Kotlin, and
Python clients. [Basecamp SDK](https://github.com/basecamp/basecamp-sdk)

The REST API requires OAuth and an identifying `User-Agent`. Collection
endpoints paginate through the HTTP `Link` header. Clients must use `ETag` or
`Last-Modified` freshness headers where supplied. Limits are dynamic. The first
limit commonly encountered is currently 50 requests per 10 seconds per IP;
clients must honor `429` and `Retry-After`. [Basecamp API overview](https://github.com/basecamp/bc-api#the-basecamp-api)

### Events and webhooks

Basecamp webhooks are registered per project. They can cover comments,
messages, to-dos, card-table records, documents, uploads, schedule entries, and
other documented recording types. Chat is explicitly excluded from webhooks.
Basecamp retries failed deliveries up to ten times with increasing delays and
then deactivates the webhook. Redirect responses do not count as success.
[Basecamp webhooks](https://github.com/basecamp/bc-api/blob/master/sections/webhooks.md)

The public webhook registration contract accepts a payload URL and event
types. It does not document a signing secret. Basecamp's own experimental
connector treats webhook posts as forgeable, uses an unguessable URL path,
re-fetches the event through the API, re-checks the author and mention, and
deduplicates by event ID. Use the same security boundary. Do not execute raw
webhook text. [Built-in support analysis](https://github.com/basecamp/basecamp-local-agent-connector/blob/main/docs/built-in-support.md)
and [connector security model](https://github.com/basecamp/basecamp-local-agent-connector#security-mechanisms)

## Existing public projects

### First-party Basecamp projects

| Project | What it proves | Reuse decision |
| --- | --- | --- |
| [`basecamp/basecamp-local-agent-connector`](https://github.com/basecamp/basecamp-local-agent-connector) | An experimental Ruby bridge registers per-project webhooks, polls unsupported event surfaces, verifies events through the API, emits trusted NDJSON, and replies through a distinct real Basecamp user profile. Its included driver targets Claude Code, but the bridge is intentionally agent-neutral. | Strongest behavioral reference for the requested member identity and security boundary. Do not copy its temporary Tailscale lifecycle as the final architecture. |
| [`basecamp/openclaw-basecamp`](https://github.com/basecamp/openclaw-basecamp) | A full Basecamp channel plugin maps runtime personas to separate Basecamp accounts, keeps a person ID and OAuth token per service account, combines webhooks with polling and reconciliation, supplies Basecamp tools, and protects against bot-to-bot loops. | Closest architectural analog for a Hermes platform plugin. Study its public contracts and tests. Implement independently against Hermes interfaces. |
| [`basecamp/basecamp-cli`](https://github.com/basecamp/basecamp-cli) | Official command, skill, profile, structured-output, and MCP surface. | Use for setup, identity verification, diagnostics, and an initial tool proof. |
| [`basecamp/basecamp-sdk`](https://github.com/basecamp/basecamp-sdk) | Official typed API clients and OpenAPI description. | Use the Python SDK in the Hermes plugin unless an SDK gap requires a small raw REST call. |

The local connector labels itself experimental and says that proper first-class
support is being built. Its design document describes the desired future Agent
Channel, but that document is a proposal, not a supported public API.
[Connector warning](https://github.com/basecamp/basecamp-local-agent-connector#basecamp-agent-connector-experimental)
and [built-in support proposal](https://github.com/basecamp/basecamp-local-agent-connector/blob/main/docs/built-in-support.md)

### Third-party projects

| Project | Assessment |
| --- | --- |
| [`stefanoverna/basecamp-mcp`](https://github.com/stefanoverna/basecamp-mcp) | Local MCP server with browser OAuth and one saved Basecamp credential set. Useful prior art for MCP ergonomics, but the official Basecamp CLI now supplies MCP and profiles. |
| [`vapvarun/basecamp-mcp-server`](https://github.com/vapvarun/basecamp-mcp-server) | Broad TypeScript MCP surface with local token configuration. It is another one-token identity and not an inbound Basecamp channel. |
| [`georgeantonopoulos/Basecamp-MCP-Server`](https://github.com/georgeantonopoulos/Basecamp-MCP-Server) | Python/FastMCP implementation with a broad Basecamp 3 tool surface. It is useful for endpoint coverage comparison, but it duplicates official SDK and CLI work. |
| [Composio Basecamp for Hermes](https://composio.dev/toolkits/basecamp/framework/hermes-agent) | The only targeted search result that directly documents Hermes plus Basecamp. It provides hosted MCP and managed OAuth, but its documented model connects the user's account and acts on the user's behalf. It does not meet the identity rule unless each agent separately authorizes its own member account. It also adds a hosted auth and data processor. |

No public Hermes-native Basecamp platform adapter surfaced in targeted web and
GitHub searches on 2026-09-03. This is a search result, not proof that none
exists. The direct Hermes result was Composio's MCP route. The Basecamp-owned
OpenClaw channel and local connector are the more useful public references.

## Recommended clean-room architecture

Build one standalone Hermes `kind: platform` plugin. Hermes recommends the
plugin path for third-party platforms and defines `connect()`, `disconnect()`,
`send()`, and inbound `handle_message()` as the adapter boundary. This avoids a
large Hermes core patch. [Hermes platform adapter guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/adding-platform-adapters.md)

Use this structure:

```text
Basecamp projects
  -> project webhooks -> shared ingress verifier -> per-agent dispatch queue
  -> chat and gap pollers -----------------------^

per-agent dispatch queue
  -> Hermes Basecamp platform adapter
  -> Hermes session keyed by Basecamp account + project + recording
  -> response through that agent's OAuth credential
  -> Basecamp comment, chat line, check-in answer, or Ping
```

Recommended boundaries:

- **One Basecamp account configuration per Hermes agent.** Store expected
  Basecamp account ID, Person ID, display name, email, and a refreshable OAuth
  credential. Never accept a mutable profile name as the only identity check.
- **One Hermes persona to one Basecamp member.** Do not fall back to the
  operator's credential. If the configured member is missing, expired, or does
  not match the expected Person ID and email, fail closed.
- **Shared ingress, isolated execution.** A central receiver can own webhook
  routes and routing metadata. It should not execute agent work. Route a
  verified pointer to the agent runtime; let that runtime fetch context and
  write with only its own credential.
- **Pointer-based events.** Treat an inbound notification as a trigger plus
  Basecamp IDs and URLs. Fetch the canonical recording, thread, author,
  assignment state, and mention attachments before dispatch.
- **Addressed events only.** Dispatch direct mentions, Pings, assignments, and
  replies in a thread to which the agent is subscribed. Make ambient project
  activity opt-in and quiet by default.
- **Session key.** Use `{basecamp_account_id, agent_person_id, project_id,
  recording_id}`. This keeps one Basecamp thread in one Hermes conversation and
  prevents context crossing between agents or projects.
- **Loop controls.** Ignore the receiving agent's own events. Rate-limit events
  from other configured agents. Deduplicate by Basecamp event ID and persist the
  cursor before visible work starts.
- **Write verification.** Read back every important write by ID. A successful
  HTTP response is not sufficient when the response can contain stale fields.
- **Tool policy.** Start with reads, comments, chat replies, to-do completion,
  and assignment-aware work. Do not initially expose project creation, people
  management, webhook management, deletion, or account administration.

## Delivery strategy

Use two stages rather than designing the full channel before proving identity:

### Stage 1: identity and outbound proof

1. Have the owner create and control a dedicated canary mailbox, then create
   one test agent member through the
   supported administrator flow. This address is a proposed setup target. This
   research did not verify that the mailbox or Basecamp account exists.
2. Create a named Basecamp CLI profile while logged in as that agent.
3. Verify `basecamp me --profile <agent>` against the expected Person ID and
   email.
4. Attach `basecamp mcp` to one isolated Hermes profile, or call the CLI in
   structured noninteractive mode.
5. Read one test recording and post one test comment. Verify in Basecamp that
   the comment author is the agent member and not the operator.

This stage answers the highest-risk question with little custom code.

### Stage 2: native Hermes channel

Implement the standalone platform plugin with the official Python SDK. Add a
small webhook receiver, authoritative re-fetch, durable deduplication, chat and
gap polling, routing, session persistence, reply delivery, and read-back
verification. Model multi-agent configuration after the public OpenClaw
channel's personas-to-accounts mapping, but use Hermes's native plugin and
adapter contracts. [OpenClaw Basecamp channel](https://github.com/basecamp/openclaw-basecamp)
and [Hermes plugin guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/plugins/index.md)

## Risks and unresolved items

- **Future product overlap.** Basecamp says proper first-class agent support is
  being built, and its pricing page says official connectors and full MCP
  support are coming soon. A custom webhook and polling transport may become
  obsolete. Keep Basecamp transport behind a narrow interface.
  [Connector warning](https://github.com/basecamp/basecamp-local-agent-connector#basecamp-agent-connector-experimental)
  and [Basecamp pricing](https://basecamp.com/pricing)
- **Full-member authority is broad.** The user requirement grants project and
  people-management capabilities. Plugin tool allowlists are necessary but are
  not a Basecamp permission boundary. [Basecamp 5 permissions](https://5.basecamp-help.com/article/1171-permissions)
- **Webhook authenticity is incomplete.** The public API does not document a
  delivery signature. Use a random path, HTTPS, immediate API corroboration,
  and short event retention. Ask Basecamp support whether a supported signing
  contract is planned. [Basecamp webhooks](https://github.com/basecamp/bc-api/blob/master/sections/webhooks.md)
- **Chat needs polling.** Chat is excluded from Basecamp webhooks. Do not claim
  real-time complete delivery until a live Agent Channel exists or polling and
  reconciliation have passed a missed-event test. [Basecamp webhooks](https://github.com/basecamp/bc-api/blob/master/sections/webhooks.md)
- **Human bootstrap remains.** Each member invitation and initial OAuth grant
  needs interactive identity access. Token refresh can then be automated.
  [Inviting people](https://5.basecamp-help.com/article/1060-inviting-people)
  and [Basecamp authentication](https://github.com/basecamp/bc-api/blob/master/sections/authentication.md)
- **Plan limits count every person.** Free permits five users and Freelancer
  permits twenty. Studio and higher advertise unlimited users without per-user
  fees. Verify the account's current plan before provisioning the full roster.
  [Basecamp pricing](https://basecamp.com/pricing)

## Recommended next action

Approve a bounded proof with one agent, one new mailbox, one sandbox Basecamp
project, and one Hermes profile. Its verifier should require all of these:

1. Basecamp shows the agent as a full team member in the primary company.
2. The agent's CLI or SDK credential resolves to the expected Person ID and
   email.
3. A Hermes-issued test comment appears under the agent's name, never under the
   human operator.
4. The agent receives one verified mention event and ignores a forged request
   sent directly to the webhook URL.
5. The agent ignores its own reply and does not loop.
6. Revoking the agent's project access or OAuth grant makes the integration
   fail closed.

If this proof passes, write the Hermes plugin architecture and implementation
plan. Keep the identity contract fixed: one Hermes agent, one Basecamp member,
one mailbox, one Person ID, and one OAuth credential.

## Research scope

This was a clean-room review of public Basecamp, Hermes, GitHub, and vendor
sources. The prior internal Basecamp implementation was not inspected or used.
