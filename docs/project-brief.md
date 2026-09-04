# Project brief

## Outcome

Build a clean-room native integration that lets Hermes agents participate in
Basecamp as distinct full members. Each agent has its own email address,
Basecamp member record, OAuth credentials, permissions, and visible authorship.

## Reference canary

- Agent: one local Hermes profile
- Email: a dedicated agent mailbox
- Basecamp role: full team member, not client and not chatbot
- Test surface: one dedicated Basecamp test project with synthetic content

## Verifier

The project passes when all of these checks pass:

1. Basecamp reports the authenticated identity as the agent's member account.
2. The agent receives one approved test event without polling another member's
   private work.
3. The agent performs one approved, reversible action in the test project.
4. A separate Basecamp read-back attributes the result to the agent's member ID
   and email, not to the human owner or another agent.
5. Revoking the agent's OAuth grant stops access without affecting other members
   or Hermes profiles.
6. No secret or Basecamp content is present in Git or test logs.

## Boundaries

- This repository starts over. Do not copy code or design from the canceled
  internal Basecamp integration.
- Mailbox creation, member invitation, invitation acceptance, OAuth approval,
  webhook creation, and Basecamp writes are external gates.
- The existing local Hermes gateway is a test target, not disposable test
  infrastructure.

## Initial work packages

1. Confirm the member and OAuth identity contract against Basecamp's official
   tools and API.
2. Define the smallest Hermes adapter boundary around the official Basecamp
   CLI or SDK.
3. Implement isolated profile selection, event intake, action policy, and
   read-back verification.
4. Run the reference canary, then document revocation and rollback evidence.
