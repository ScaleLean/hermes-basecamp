import unittest

from event_model import is_addressed_to, normalize_event, parse_target


class EventModelTests(unittest.TestCase):
    def test_normalizes_comment_to_parent_recording(self):
        event = normalize_event(
            {
                "id": 77,
                "kind": "comment_created",
                "created_at": "2026-09-04T01:00:00Z",
                "creator": {"id": 11, "name": "Human User"},
                "bucket": {"id": 22},
                "recording": {"id": 33, "type": "Comment", "content": "<p>Hello</p>"},
                "parent": {"id": 44},
            }
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.chat_id, "bucket:22/recording:44")
        self.assertIn("Hello", event.text)

    def test_normalizes_chat_line_to_chat_target(self):
        event = normalize_event(
            {
                "id": 77,
                "kind": "chat_transcript_created",
                "creator": {"id": 11, "name": "Human User"},
                "bucket": {"id": 22},
                "recording": {"id": 33, "type": "Chat::Line", "content": "Hi"},
                "parent": {"id": 44},
            }
        )
        self.assertEqual(event.chat_id, "bucket:22/recording:44")

    def test_rejects_incomplete_events(self):
        self.assertIsNone(normalize_event({"id": 1}))

    def test_target_parser_rejects_ambiguous_targets(self):
        with self.assertRaises(ValueError):
            parse_target("general")
        with self.assertRaises(ValueError):
            parse_target("chat:not-a-number:2")

    def test_target_parser_accepts_explicit_target(self):
        self.assertEqual(parse_target("chat:22:44"), ("chat", "22", "44"))

    def test_structured_person_mention_is_addressed(self):
        raw = {
            "recording": {
                "content": (
                    'Can <bc-attachment content-type="application/vnd.basecamp.mention" '
                    'sgid="sgid://bc3/Person/123">Hermes Agent</bc-attachment> check this?'
                )
            }
        }
        self.assertTrue(is_addressed_to(raw, person_id="123", mention="@HermesAgent"))

    def test_signed_structured_mention_uses_nested_person_id(self):
        raw = {
            "recording": {
                "content": (
                    '<p><bc-attachment content-type="application/vnd.basecamp.mention" '
                    'sgid="opaque-signed-global-id"><figure>'
                    '<img data-avatar-for-person-id="123" alt="Hermes Agent">'
                    "</figure></bc-attachment></p>"
                )
            }
        }
        self.assertTrue(is_addressed_to(raw, person_id="123", mention="@HermesAgent"))

    def test_plain_text_name_does_not_authorize(self):
        raw = {"recording": {"content": "Can @HermesAgent check this?"}}
        self.assertFalse(is_addressed_to(raw, person_id="123", mention="@HermesAgent"))

    def test_other_person_mention_does_not_authorize(self):
        raw = {
            "recording": {
                "content": (
                    '<bc-attachment content-type="application/vnd.basecamp.mention" '
                    'sgid="sgid://bc3/Person/999">Hermes Agent</bc-attachment>'
                )
            }
        }
        self.assertFalse(is_addressed_to(raw, person_id="123", mention="@HermesAgent"))

    def test_explicit_assignment_is_addressed(self):
        raw = {"recording": {"assignees": [{"id": 123}]}}
        self.assertTrue(is_addressed_to(raw, person_id="123", mention="@HermesAgent"))

    def test_ambient_activity_is_not_addressed(self):
        raw = {"recording": {"content": "General project update"}}
        self.assertFalse(is_addressed_to(raw, person_id="123", mention="@HermesAgent"))

    def test_ping_from_non_client_member_is_a_direct_trigger(self):
        raw = {
            "id": 91,
            "kind": "ping_line_created",
            "created_at": "2026-09-04T01:00:00Z",
            "creator": {"id": 11, "name": "Teammate", "client": False},
            "bucket": {"id": 55, "type": "Circle"},
            "recording": {"id": 92, "type": "Chat::Line", "content": "Hello"},
            "parent": {"id": 93, "type": "Chat::Transcript"},
        }
        normalized = normalize_event(raw)
        self.assertEqual(normalized.chat_id, "ping:55")
        self.assertTrue(is_addressed_to(raw, person_id="123", mention="@HermesAgent"))

    def test_ping_from_client_is_rejected(self):
        raw = {
            "creator": {"id": 11, "client": True},
            "bucket": {"id": 55, "type": "Circle"},
            "recording": {"id": 92, "type": "Chat::Line", "content": "Hello"},
        }
        self.assertFalse(is_addressed_to(raw, person_id="123", mention="@HermesAgent"))

    def test_official_target_grammar(self):
        self.assertEqual(parse_target("recording:44"), ("recording", "", "44"))
        self.assertEqual(parse_target("bucket:22"), ("bucket", "22", ""))
        self.assertEqual(parse_target("bucket:22/recording:44"), ("recording", "22", "44"))
        self.assertEqual(parse_target("ping:55"), ("ping", "", "55"))
