import unittest

from formatter import format_chunks, format_message, render_person_mention


class FormatterTests(unittest.TestCase):
    def test_formats_supported_markdown(self):
        rendered = format_message("# Status\n\n**Done** and *checked*.\n\n- one\n- two")
        self.assertIn("<h1>Status</h1>", rendered)
        self.assertIn("<strong>Done</strong>", rendered)
        self.assertIn("<em>checked</em>", rendered)
        self.assertIn("<ul>", rendered)

    def test_escapes_raw_html(self):
        rendered = format_message('<script>alert("x")</script>')
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_rejects_unsafe_link_scheme(self):
        rendered = format_message("[click](javascript:alert(1))")
        self.assertNotIn("href=", rendered)
        self.assertIn("click", rendered)

    def test_preserves_safe_https_link(self):
        rendered = format_message("[Basecamp](https://basecamp.com/agents)")
        self.assertIn('href="https://basecamp.com/agents"', rendered)

    def test_chunks_rendered_payloads_to_limit(self):
        chunks = format_chunks("alpha " * 500, max_length=300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 300 for chunk in chunks))

    def test_renders_structured_person_mention(self):
        rendered = render_person_mention("123", "Hermes Agent & Co")
        self.assertIn('sgid="sgid://bc3/Person/123"', rendered)
        self.assertIn("Hermes Agent &amp; Co", rendered)

    def test_rejects_non_numeric_person_id(self):
        with self.assertRaises(ValueError):
            render_person_mention("123/../../", "Hermes Agent")
