# Capability and policy contract

Hermes Basecamp exposes ten stable tools. Focused tools cover common work. Two
generic tools map official API paths to typed SDK methods through the frozen
`sdk_routes.py` inventory.

## Policy classes

| Class | Rule |
| --- | --- |
| `read` | Requires verified identity, membership, explicit project scope, and canonical ownership. |
| `ordinary_write` | Also requires a direct mention, assignment, or approved schedule, plus an idempotency key and read-back. |
| `sensitive_write` | Also requires unexpired approval for the exact operation, project, and arguments. |
| `adminland_denied` | Never automated. |

Sensitive work includes access changes, archive, trash, deletion, subscription
changes, and actions with people or visibility side effects. Billing, account
ownership, security, member creation, invitations, invitation acceptance, and
employee or client classification remain human-controlled.

## Inventories

- `policy.py` classifies 195 project-member capabilities: 88 reads, 57 ordinary
  writes, 44 sensitive writes, and six Adminland-denied operations.
- `sdk_routes.py` exposes the 189 non-Adminland capabilities at their official
  SDK paths.
- `sdk_inventory.py` classifies all 274 public methods in SDK 0.16 as public,
  internal, or excluded.
- `scripts/generate_sdk_routes.py --check` detects route drift.

Generic tools require `bucket_id` as explicit project scope. They reject URLs,
query strings embedded in paths, traversal, scope mismatches, and unknown
routes. Query and body data still pass SDK signature validation.

## Mutation and reply recovery

Domain mutations reserve an idempotency key before dispatch. A verified result
stores its SDK response and canonical evidence. A post-dispatch failure becomes
uncertain. `journal list` shows unresolved entries. `journal reconcile` releases
one exact entry only after proof that Basecamp did not apply it.

Conversation replies use a separate delivery journal because the content must
survive a gateway restart. Recovery searches canonical Basecamp content and
agent authorship before retrying only the saved write. It does not repeat the
Hermes run.

## Media

Outbound files must be under an explicit `BASECAMP_MEDIA_ROOTS` path. The media
pipeline rejects empty files, excess size, disallowed MIME types, and invalid
SDK attachment identifiers. Inbound media goes to a separate owner-only spool
that is not an implicit upload root.

Webhook types are limited to recordings with an exact SDK getter that can prove
project ownership. The receiver checks its path token before JSON parsing and
returns a retryable status for transient refetch or queue failures.
