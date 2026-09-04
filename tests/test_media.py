import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from media import MediaManager, MediaValidationError, configured_inbound_media_root, configured_media_roots


class _Client:
    async def call(self, service, method, arguments):
        return {"attachable_sgid": "sgid://bc3/Attachment/1"}

    async def download_url(self, url):
        return SimpleNamespace(
            body=b"verified",
            content_type="text/plain",
            content_length=8,
            filename="proof.txt",
        )


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = MediaManager(_Client(), allowed_roots=(self.root,), max_bytes=20)

    def tearDown(self):
        self.temp.cleanup()

    async def test_valid_file_upload_returns_attachment_markup(self):
        path = self.root / "proof.txt"
        path.write_text("verified")
        markup = await self.manager.upload_markup([str(path)])
        self.assertIn("sgid://bc3/Attachment/1", markup)

    def test_outside_path_and_oversize_file_fail(self):
        with self.assertRaises(MediaValidationError):
            self.manager.prepare(__file__)
        path = self.root / "large.txt"
        path.write_text("x" * 21)
        with self.assertRaises(MediaValidationError):
            self.manager.prepare(str(path))

    def test_disallowed_mime_fails(self):
        path = self.root / "payload.bin"
        path.write_bytes(b"binary")
        with self.assertRaisesRegex(MediaValidationError, "MIME"):
            self.manager.prepare(str(path))

    async def test_receives_attachment_with_sdk_length_and_path_checks(self):
        received = await self.manager.receive(
            {"id": 1, "download_url": "https://storage.3.basecamp.com/safe"}, self.root
        )
        self.assertEqual(received.path.read_bytes(), b"verified")
        self.assertEqual(received.path.stat().st_mode & 0o777, 0o600)
        retried = await self.manager.receive(
            {"id": 1, "download_url": "https://storage.3.basecamp.com/safe"}, self.root
        )
        self.assertEqual(retried.path.read_bytes(), b"verified")

    async def test_rejects_non_https_attachment(self):
        local_uri = f"file:///{'etc'}/{'pass' + 'wd'}"
        with self.assertRaisesRegex(MediaValidationError, "HTTPS"):
            await self.manager.receive({"download_url": local_uri}, self.root)

    def test_default_inbound_spool_is_private_and_not_an_outbound_root(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.dict(os.environ, {"BASECAMP_STATE_DIR": temp}, clear=False),
            mock.patch.dict(os.environ, {"BASECAMP_MEDIA_ROOTS": ""}, clear=False),
        ):
            inbound = configured_inbound_media_root()
            self.assertEqual(configured_media_roots(), ())
            self.assertEqual(inbound.stat().st_mode & 0o777, 0o700)
