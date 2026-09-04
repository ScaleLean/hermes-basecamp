# Hermes Basecamp 1.0 release gate

Version 1.0 requires one continuous 24-hour run with three separate full
members: Maximus, a second Hermes identity, and a non-Hermes conductor. The run
uses one allowed synthetic project and one denied control project.

## Twenty qualifying events

1. Ping Maximus.
2. Ping Agent B.
3. Continue a Ping.
4. Mention Maximus in Campfire.
5. Mention Agent B in Campfire.
6. Prove busy and quiet Campfire cursor isolation.
7. Assign a to-do to Maximus.
8. Observe one long-running acknowledgement.
9. Observe a result comment and completion.
10. Follow up on assigned work.
11. Assign a card.
12. Move a card.
13. Answer a check-in.
14. Mention the agent on a message.
15. Receive an attachment.
16. Send an attachment.
17. Deliver the same event through webhook and polling.
18. Restart the gateway with pending work.
19. Disable webhooks and prove polling recovery.
20. Prove denied-project and agent-loop isolation.

## Acceptance

- Zero lost eligible events.
- Zero duplicate Hermes runs or replies.
- Zero wrong-member attribution.
- Zero denied-project content in Hermes.
- Ping and Campfire first responses within 30 seconds.
- Assignments complete or acknowledge within 60 seconds.
- Every mutation has independent canonical read-back.
- All 20 events qualify in one continuous 24-hour period.

The report contains only aggregate timing and pass/fail evidence. It contains no
credentials, live IDs, or message content. Invitations, access changes, archive,
trash, and deletion are outside the automated suite.
