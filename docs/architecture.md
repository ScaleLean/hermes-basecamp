# Architecture

## Core decision

Use Basecamp's official async Python SDK for runtime work. Use its CLI only for
interactive setup and independent diagnostics. Keep the plugin standalone until
the live 1.0 gate passes.

One Hermes home and one gateway map to one dedicated full Basecamp member. No
credential fallback crosses that boundary.

## Inbound pipeline

```text
webhook pointer -----------+
                           v
Campfire polling ------> canonical SDK refetch
Ping polling ----------> project and actor gate
notification polling --> transactional SQLite inbox
assignment snapshots ---> leased event
project activity -------> Hermes run
                           verified outbound write
                           terminal tombstone
```

Each lane has independent timing and health. Webhook payloads are untrusted
pointers. The adapter rebuilds actor, project, type, parent, content, and
assignment data from Basecamp before eligibility checks.

Polling cursors and accepted events commit together. Timestamp streams retain
all IDs at the high watermark. Assignment polling is a snapshot, not a timeline.
The inbox accepts one event per continuous active-assignment generation. A
missing assignment closes its generation. A later reassignment opens the next
generation. Comment changes do not create assignment events.

Workers lease inbox rows. Expired leases return to pending after restart.
Terminal rows retain no message body and remain as duplicate tombstones for 30
days. Poison rows make health `recovering` until an operator resolves them.

## Delivery pipeline

Before a reply write, the adapter stores target, content, purpose, and sequence
in a private SQLite delivery journal. It then marks dispatch, calls the typed SDK
service, and reads the result back through Basecamp. The creator must match the
configured agent member.

If dispatch or read-back is uncertain, the inbox event remains pending. On the
next attempt the adapter searches the canonical target for the exact content by
the agent member. If found, it records verification. If not found, it retries the
saved write only. Hermes does not run again. Acknowledgements are not mistaken
for final results.

## Identity and authorization

Startup checks the configured account, person ID, email, `employee=true`,
`client=false`, and membership in every allowlisted project. Mutations repeat the
identity attestation. Resource ownership comes from canonical Basecamp reads.

Each profile also has an explicit peer-agent person-ID roster. A peer-authored
event can trigger the agent only through a structured mention or assignment.
Ping lines, ordinary replies, check-in activity, and active-recording follow-ups
from peers remain quiet. This prevents autonomous reply loops while preserving
human follow-up continuity.

Ordinary work requires trusted direct interaction, assignment, or an approved
schedule. Sensitive changes need action-time approval for the exact operation,
project, and argument digest. Adminland stays unavailable.

## Generic API surface

`sdk_routes.py` is generated from the installed official SDK and the reviewed
policy registry. It maps exact HTTP method and official API path to one typed SDK
capability. The generic tools do not send raw HTTP.

The caller supplies an explicit allowlisted project scope even for official
paths that omit a bucket ID. The resource index then proves ownership. Paths
with a bucket must match the explicit scope. Unknown methods and paths fail
closed.

## Production ingress

The embedded webhook receiver binds to loopback. A user-managed HTTPS proxy or
tunnel exposes the tokenized path. `webhooks sync` creates or repairs one scoped
registration for each allowlisted project. Polling remains the correctness
mechanism during webhook delay or outage.

## Upstream path

The external plugin must pass the 24-hour live gate before a 1.0 tag. Hermes
bundling or catalog inclusion is separate future work. There must never be two
divergent implementations.
