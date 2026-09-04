# Capability and policy contract

Hermes Basecamp exposes eight compact domain tools. Each tool accepts an
explicit operation enum. The enum maps to one method in `basecamp-sdk` 0.16.
Unknown operations fail closed.

## Policy classes

| Class | Allowed context |
| --- | --- |
| `read` | An allowlisted project after identity, role, membership, and canonical ownership checks. |
| `ordinary_write` | A trusted Basecamp session target or approved Basecamp cron delivery target for the same project. Hermes task-local session context supplies this proof. An idempotency key and verified read-back are required. |
| `sensitive_write` | An unexpired action-time approval for the exact operation, project, and argument digest. |
| `adminland_denied` | Never automated. |

Sensitive operations include delete, trash, archive, restore, project access,
and cross-project card moves. Billing, account ownership, account security,
employee or client classification, and invitation acceptance remain in
Basecamp Adminland.

The complete runtime inventory is in `policy.py`. The SDK inventory test fails
if an allowed registry entry does not exist in the pinned SDK range.

## Recovery model

Every mutation reserves a profile-local idempotency key in a SQLite journal.
A verified mutation records the SDK result and canonical read-back. A transport
or read-back failure after dispatch records an uncertain result. Hermes must
reconcile an uncertain item before retrying it. It must not assume that a
timed-out call failed.

Webhook events use a bounded SQLite queue. Duplicate IDs remain recorded after
acknowledgement. An interrupted event returns to the pending queue after a
restart. Repeated failures move one event to the poison state without stopping
other events. Polling lanes have independent circuit state and durable content-
free cursors.

## Media

Set `BASECAMP_MEDIA_ROOTS` to a path-separated list of roots. Uploads reject
paths outside these roots, empty files, files larger than the configured limit,
and MIME types outside the allowlist. Hermes verifies that the SDK returns a
valid attachment SGID before it includes the attachment in a message.

Inbound downloads use a separate owner-only spool below `BASECAMP_STATE_DIR`
or the Hermes state directory. That spool is not an implicit outbound upload
root. Set `BASECAMP_MEDIA_ROOTS` explicitly for outbound delivery.

The raw webhook receiver must bind to loopback and sit behind a trusted TLS
reverse proxy. A non-loopback bind requires the explicit
`BASECAMP_WEBHOOK_TLS_PROXY=true` assertion. The receiver validates the path
token before parsing JSON and returns 503 for transient canonical-refetch or
queue failures so Basecamp can retry.

Webhook reconciliation permits only official types with a read-only SDK getter
that proves project ownership from the recording ID: `Comment`,
`Client::Forward`, `CloudFile`, `Document`, `GoogleDocument`, `Inbox::Forward`,
`Kanban::Card`, `Kanban::Step`, `Message`, `Question`, `Question::Answer`,
`Schedule::Entry`, `Todo`, `Todolist`, `Upload`, and `Vault`.
`Client::Approval::Response` is rejected because SDK 0.16 has no response getter
for its webhook recording ID. `Client::Reply` is rejected because its getter
also requires a parent ID that the untrusted webhook would have to supply.
`all` is rejected because it includes those unverifiable types.

## Mutation recovery

The operation journal records `reserved` before dispatch and `dispatched`
immediately before the SDK mutation. Operators can list unresolved entries.
A matching reserved entry can be explicitly released because no call began.
Legacy `pending`, `dispatched`, and `uncertain` entries require canonical
reconciliation plus explicit confirmation that Basecamp did not apply the
mutation before the exact key and argument digest can be released for retry.
The runtime never retries an uncertain mutation automatically.

Webhook terminal tombstones are retained for 30 days and capped at 5,000 rows.
Terminal pruning never removes active queued or processing rows. The durable
store remains the replay authority; the bounded in-memory seen cache only
accelerates duplicate checks.
