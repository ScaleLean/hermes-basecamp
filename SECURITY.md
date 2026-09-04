# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected credential leak, identity bypass,
project-allowlist bypass, replay flaw, or unauthorized Basecamp write.

Use GitHub's private vulnerability reporting feature for this repository. Add
the affected version, reproduction steps, impact, and any suggested fix. Do
not include live credentials or customer data.

## Supported versions

This project is currently an alpha. Security fixes are applied to the latest
commit on `main`. There is no stable release branch yet.

## Security invariants

- One Hermes profile maps to one dedicated Basecamp member.
- Every connection and write verifies account ID, person ID, and email.
- Every target must belong to the configured project allowlist.
- OAuth files must stay outside the repository and use owner-only permissions.
- Every write is read back and checked for the expected creator ID.
