import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import ProcessingOutcome, SendResult

from adapter import BasecampAdapter, _standalone_send
from webhook_ingress import DurableWebhookStore, WebhookIngress


class AdapterRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_and_disconnect_recover_webhook_leases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = SimpleNamespace(
                project_ids=("10",),
                expected=SimpleNamespace(account_id="1", person_id="7"),
                attest_full_member=AsyncMock(),
                close=AsyncMock(),
            )
            config = PlatformConfig(
                extra={
                    "account_id": "1",
                    "person_id": "7",
                    "person_email": "agent@example.com",
                    "mention": "@agent",
                    "project_ids": ["10"],
                    "access_token": "test",
                }
            )
            with (
                patch("adapter._make_client", return_value=client),
                patch("adapter.default_replay_path", return_value=root / "replay.json"),
                patch("adapter.configured_media_roots", return_value=(root,)),
                patch("adapter.configured_inbound_media_root", return_value=root),
                patch("adapter.Platform", return_value=next(iter(Platform))),
            ):
                adapter = BasecampAdapter(config)
            recover = MagicMock()
            adapter._webhooks = SimpleNamespace(recover=recover)
            adapter._poll_loop = AsyncMock()

            self.assertTrue(await adapter.connect())
            await adapter.disconnect()

            self.assertEqual(recover.call_count, 2)
            client.close.assert_awaited_once()

    async def test_denied_project_event_never_reaches_message_handler(self):
        with tempfile.TemporaryDirectory() as temp:
            client = SimpleNamespace(
                project_ids=("10",),
                expected=SimpleNamespace(account_id="1", person_id="7"),
            )
            config = PlatformConfig(
                extra={
                    "account_id": "1",
                    "person_id": "7",
                    "person_email": "agent@example.com",
                    "mention": "@agent",
                    "project_ids": ["10"],
                    "access_token": "test",
                }
            )
            with (
                patch("adapter._make_client", return_value=client),
                patch("adapter.default_replay_path", return_value=Path(temp) / "replay.json"),
                patch("adapter.configured_media_roots", return_value=(Path(temp),)),
                patch("adapter.configured_inbound_media_root", return_value=Path(temp)),
                patch("adapter.Platform", return_value=next(iter(Platform))),
            ):
                adapter = BasecampAdapter(config)
            adapter._bootstrapped = True
            adapter._poller.collect = AsyncMock(
                return_value=[
                    {
                        "id": 1,
                        "kind": "comment_created",
                        "created_at": "2026-09-04T01:00:00Z",
                        "creator": {"id": 8, "name": "Denied"},
                        "bucket": {"id": 99},
                        "recording": {"id": 40, "type": "Comment", "content": "@agent denied"},
                    }
                ]
            )

            async def complete(event):
                adapter._verified_deliveries.add(str(event.message_id))
                await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

            adapter.handle_message = AsyncMock(side_effect=complete)
            client.fetch_recording = AsyncMock()

            await adapter._poll_once()

            adapter.handle_message.assert_not_awaited()
            client.fetch_recording.assert_not_awaited()

    async def test_poison_event_does_not_abort_later_event(self):
        with tempfile.TemporaryDirectory() as temp:
            client = SimpleNamespace(
                project_ids=("10",),
                expected=SimpleNamespace(account_id="1", person_id="7"),
            )
            config = PlatformConfig(
                extra={
                    "account_id": "1",
                    "person_id": "7",
                    "person_email": "agent@example.com",
                    "mention": "@agent",
                    "project_ids": ["10"],
                    "access_token": "test",
                }
            )
            with (
                patch("adapter._make_client", return_value=client),
                patch("adapter.default_replay_path", return_value=Path(temp) / "replay.json"),
                patch("adapter.configured_media_roots", return_value=(Path(temp),)),
                patch("adapter.configured_inbound_media_root", return_value=Path(temp)),
                patch("adapter.Platform", return_value=next(iter(Platform))),
            ):
                adapter = BasecampAdapter(config)
            adapter._bootstrapped = True
            raw = lambda event_id: {
                "id": event_id,
                "kind": "comment_created",
                "created_at": f"2026-09-04T01:00:0{event_id}Z",
                "creator": {"id": 8, "name": "Member"},
                "bucket": {"id": 10},
                "recording": {
                    "id": 40 + event_id,
                    "type": "Comment",
                    "content": '<bc-attachment content-type="application/vnd.basecamp.mention" '
                    'sgid="sgid://bc3/Person/7"></bc-attachment>',
                },
            }
            adapter._poller.collect = AsyncMock(return_value=[raw(2), raw(1)])
            client.fetch_recording = AsyncMock(side_effect=[RuntimeError("poison"), raw(2)["recording"]])

            async def complete(event):
                adapter._verified_deliveries.add(str(event.message_id))
                await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

            adapter.handle_message = AsyncMock(side_effect=complete)

            await adapter._poll_once()

            self.assertEqual(client.fetch_recording.await_count, 2)
            adapter.handle_message.assert_awaited_once()

    async def test_durable_pointer_waits_for_hermes_reply_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            client = SimpleNamespace(
                project_ids=("10",),
                expected=SimpleNamespace(account_id="1", person_id="7"),
            )
            config = PlatformConfig(
                extra={
                    "account_id": "1",
                    "person_id": "7",
                    "person_email": "agent@example.com",
                    "mention": "@agent",
                    "project_ids": ["10"],
                    "access_token": "test",
                }
            )
            with (
                patch("adapter._make_client", return_value=client),
                patch("adapter.default_replay_path", return_value=Path(temp) / "replay.json"),
                patch("adapter.configured_media_roots", return_value=(Path(temp),)),
                patch("adapter.configured_inbound_media_root", return_value=Path(temp)),
                patch("adapter.Platform", return_value=next(iter(Platform))),
            ):
                adapter = BasecampAdapter(config)
            adapter._bootstrapped = True
            raw = {
                "id": 9,
                "kind": "comment_created",
                "created_at": "2026-09-04T01:00:09Z",
                "creator": {"id": 8, "name": "Member"},
                "bucket": {"id": 10},
                "recording": {
                    "id": 49,
                    "type": "Comment",
                    "content": '<bc-attachment content-type="application/vnd.basecamp.mention" '
                    'sgid="sgid://bc3/Person/7"></bc-attachment> work',
                },
            }
            adapter._poller.collect = AsyncMock(return_value=[raw])
            client.fetch_recording = AsyncMock(return_value=raw["recording"])
            delivered = asyncio.Event()

            async def delayed_handler(event):
                await delivered.wait()
                return "completed reply"

            adapter._message_handler = delayed_handler

            async def verified_send(chat_id, *_args, **_kwargs):
                adapter._verified_deliveries.add(adapter._active_dispatches[chat_id])
                return SendResult(success=True, message_id="reply-1")

            adapter.send = AsyncMock(side_effect=verified_send)
            task = asyncio.create_task(adapter._poll_once())
            for _ in range(20):
                await asyncio.sleep(0.01)
                if adapter._replay.path.exists():
                    break
            state = json.loads(adapter._replay.path.read_text())
            self.assertIn("9", state["pending"])
            self.assertNotIn("9", state["committed"])
            delivered.set()
            await task
            state = json.loads(adapter._replay.path.read_text())
            self.assertIn("9", state["committed"])

    async def test_assignment_dispatch_uses_canonical_creator(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canonical = {
                "id": 49,
                "type": "Todo",
                "title": "Test the integration",
                "creator": {"id": 8, "name": "Member", "client": False},
                "assignees": [{"id": 7}],
                "bucket": {"id": 10},
            }
            client = SimpleNamespace(
                project_ids=("10",),
                expected=SimpleNamespace(account_id="1", person_id="7"),
                fetch_recording=AsyncMock(return_value=canonical),
                call=AsyncMock(return_value={"id": 49, "completed": True, "bucket": {"id": 10}}),
                post_comment=AsyncMock(return_value={"id": 90}),
                verify_comment_authorship=AsyncMock(return_value={"id": 90, "creator": {"id": 7}}),
            )
            config = PlatformConfig(
                extra={
                    "account_id": "1",
                    "person_id": "7",
                    "person_email": "agent@example.com",
                    "mention": "@agent",
                    "project_ids": ["10"],
                    "access_token": "test",
                }
            )
            with (
                patch("adapter._make_client", return_value=client),
                patch("adapter.default_replay_path", return_value=root / "replay.json"),
                patch("adapter.configured_media_roots", return_value=(root,)),
                patch("adapter.configured_inbound_media_root", return_value=root),
                patch("adapter.Platform", return_value=next(iter(Platform))),
            ):
                adapter = BasecampAdapter(config)
            adapter._bootstrapped = True
            adapter._poller.collect = AsyncMock(
                return_value=[
                    {
                        "id": "assignment:49",
                        "kind": "assignment_created",
                        "creator": {"id": "basecamp", "name": "Basecamp"},
                        "bucket": {"id": 10},
                        "recording": {"id": 49, "type": "Todo", "assignees": [{"id": 7}]},
                    }
                ]
            )
            seen_sources = []

            async def handle(event):
                seen_sources.append(event.source)
                adapter._verified_deliveries.add(str(event.message_id))
                await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

            adapter.handle_message = AsyncMock(side_effect=handle)

            await adapter._poll_once()

            self.assertEqual([source.user_id for source in seen_sources], ["8"])
            self.assertTrue(seen_sources[0].role_authorized)

            client.call.reset_mock()
            adapter._poller.collect.return_value[0]["id"] = "assignment:49:no-reply"

            async def no_reply(event):
                await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

            adapter.handle_message.side_effect = no_reply
            await adapter._poll_once()

            client.call.assert_not_awaited()
            client.post_comment.assert_awaited_once()

    async def test_native_media_send_uses_attachment_markup_and_verified_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = root / "proof.png"
            image.write_bytes(b"png")
            client = SimpleNamespace(
                project_ids=("10",),
                expected=SimpleNamespace(account_id="1", person_id="7"),
                attest_full_member=AsyncMock(),
                call=AsyncMock(return_value={"attachable_sgid": "sgid://bc3/Attachment/55"}),
                post_chat=AsyncMock(return_value={"id": 66}),
                verify_chat_authorship=AsyncMock(return_value={"creator": {"id": 7}}),
            )
            config = PlatformConfig(
                extra={
                    "account_id": "1",
                    "person_id": "7",
                    "person_email": "agent@example.com",
                    "mention": "@agent",
                    "project_ids": ["10"],
                    "access_token": "test",
                }
            )
            with (
                patch("adapter._make_client", return_value=client),
                patch("adapter.default_replay_path", return_value=root / "replay.json"),
                patch("adapter.configured_media_roots", return_value=(root,)),
                patch("adapter.configured_inbound_media_root", return_value=root),
                patch("adapter.Platform", return_value=next(iter(Platform))),
            ):
                adapter = BasecampAdapter(config)

            result = await adapter.send_image_file("chat:10:30", str(image), "Proof")

            self.assertTrue(result.success)
            sent = client.post_chat.await_args.args[2]
            self.assertIn("Proof", sent)
            self.assertIn("<bc-attachment", sent)

    async def test_cancelled_dispatch_releases_only_its_webhook_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canonical = lambda event_id: {
                "id": event_id,
                "kind": "comment_created",
                "created_at": f"2026-09-04T01:00:{event_id:02d}Z",
                "creator": {"id": 8, "name": "Member"},
                "bucket": {"id": 10},
                "recording": {
                    "id": 40 + event_id,
                    "type": "Comment",
                    "content": '<bc-attachment content-type="application/vnd.basecamp.mention" '
                    'sgid="sgid://bc3/Person/7"></bc-attachment>',
                },
                "parent": {"id": 40},
            }
            client = SimpleNamespace(
                project_ids=("10",),
                expected=SimpleNamespace(account_id="1", person_id="7"),
                fetch_webhook_event=AsyncMock(side_effect=lambda payload: canonical(int(payload["id"]))),
                fetch_recording=AsyncMock(side_effect=lambda payload: payload["recording"]),
            )
            config = PlatformConfig(
                extra={
                    "account_id": "1",
                    "person_id": "7",
                    "person_email": "agent@example.com",
                    "mention": "@agent",
                    "project_ids": ["10"],
                    "access_token": "test",
                }
            )
            with (
                patch("adapter._make_client", return_value=client),
                patch("adapter.default_replay_path", return_value=root / "replay.json"),
                patch("adapter.configured_media_roots", return_value=(root,)),
                patch("adapter.configured_inbound_media_root", return_value=root),
                patch("adapter.Platform", return_value=next(iter(Platform))),
            ):
                adapter = BasecampAdapter(config)
            adapter._bootstrapped = True
            adapter._poller.collect = AsyncMock(return_value=[])
            adapter._webhooks = WebhookIngress(
                client, token="t" * 32, store=DurableWebhookStore(root / "webhooks.sqlite3")
            )
            await adapter._webhooks.ingest("t" * 32, {"id": 10})
            await adapter._webhooks.ingest("t" * 32, {"id": 11})
            adapter.handle_message = AsyncMock(side_effect=asyncio.CancelledError)

            with self.assertRaises(asyncio.CancelledError):
                await adapter._poll_once()

            leased = []
            for _ in range(2):
                batch = await adapter._webhooks.drain_nowait()
                self.assertEqual(len(batch), 1)
                leased.append(batch[0]["id"])
                adapter._webhooks.ack(batch[0])
            self.assertEqual(set(leased), {10, 11})

    async def test_denied_durable_event_is_acked_not_stranded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = SimpleNamespace(
                project_ids=("10",),
                expected=SimpleNamespace(account_id="1", person_id="7"),
            )
            config = PlatformConfig(
                extra={
                    "account_id": "1",
                    "person_id": "7",
                    "person_email": "agent@example.com",
                    "mention": "@agent",
                    "project_ids": ["10"],
                    "access_token": "test",
                }
            )
            with (
                patch("adapter._make_client", return_value=client),
                patch("adapter.default_replay_path", return_value=root / "replay.json"),
                patch("adapter.configured_media_roots", return_value=(root,)),
                patch("adapter.configured_inbound_media_root", return_value=root),
                patch("adapter.Platform", return_value=next(iter(Platform))),
            ):
                adapter = BasecampAdapter(config)
            adapter._bootstrapped = True
            adapter._poller.collect = AsyncMock(return_value=[])
            store = DurableWebhookStore(root / "webhooks.sqlite3")
            adapter._webhooks = WebhookIngress(client, token="t" * 32, store=store)
            store.put(
                "20",
                {
                    "id": 20,
                    "kind": "comment_created",
                    "created_at": "2026-09-04T01:00:20Z",
                    "creator": {"id": 8},
                    "bucket": {"id": 99},
                    "recording": {"id": 60, "type": "Comment", "content": "denied"},
                },
            )

            await adapter._poll_once()

            self.assertIsNone(store.next())

    async def test_canonical_inbound_attachment_maps_to_message_event_media(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = SimpleNamespace(
                project_ids=("10",),
                expected=SimpleNamespace(account_id="1", person_id="7"),
                fetch_recording=AsyncMock(),
            )
            config = PlatformConfig(
                extra={
                    "account_id": "1",
                    "person_id": "7",
                    "person_email": "agent@example.com",
                    "mention": "@agent",
                    "project_ids": ["10"],
                    "access_token": "test",
                }
            )
            with (
                patch("adapter._make_client", return_value=client),
                patch("adapter.default_replay_path", return_value=root / "replay.json"),
                patch("adapter.configured_media_roots", return_value=(root,)),
                patch("adapter.configured_inbound_media_root", return_value=root),
                patch("adapter.Platform", return_value=next(iter(Platform))),
            ):
                adapter = BasecampAdapter(config)
            raw = {
                "id": 31,
                "kind": "comment_created",
                "created_at": "2026-09-04T01:00:31Z",
                "creator": {"id": 8, "name": "Member"},
                "bucket": {"id": 10},
                "recording": {
                    "id": 61,
                    "type": "Comment",
                    "content": '<bc-attachment content-type="application/vnd.basecamp.mention" '
                    'sgid="sgid://bc3/Person/7"></bc-attachment>',
                },
            }
            client.fetch_recording.return_value = raw["recording"]
            adapter._bootstrapped = True
            adapter._poller.collect = AsyncMock(return_value=[raw])
            adapter._receive_media = AsyncMock(
                return_value=[SimpleNamespace(path=root / "proof.pdf", mime_type="application/pdf")]
            )

            async def complete(event):
                adapter._verified_deliveries.add(str(event.message_id))
                await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

            adapter.handle_message = AsyncMock(side_effect=complete)
            await adapter._poll_once()
            event = adapter.handle_message.await_args.args[0]
            self.assertEqual(event.media_urls, [str(root / "proof.pdf")])
            self.assertEqual(event.media_types, ["application/pdf"])
            self.assertEqual(event.media_text_inlined, [False])

    async def test_send_and_standalone_cover_chat_and_comment_with_readback(self):
        client = SimpleNamespace(
            project_ids=("10",),
            expected=SimpleNamespace(account_id="1", person_id="7"),
            attest_full_member=AsyncMock(),
            close=AsyncMock(),
            post_chat=AsyncMock(return_value={"id": 71}),
            post_comment=AsyncMock(return_value={"id": 72}),
            verify_chat_authorship=AsyncMock(return_value={"id": 71, "creator": {"id": 7}}),
            verify_comment_authorship=AsyncMock(return_value={"id": 72, "creator": {"id": 7}}),
        )
        config = PlatformConfig(
            extra={
                "account_id": "1",
                "person_id": "7",
                "person_email": "agent@example.com",
                "mention": "@agent",
                "project_ids": ["10"],
                "access_token": "test",
            }
        )
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("adapter._make_client", return_value=client),
            patch("adapter.default_replay_path", return_value=Path(temp) / "replay.json"),
            patch("adapter.configured_inbound_media_root", return_value=Path(temp)),
            patch("adapter.Platform", return_value=next(iter(Platform))),
        ):
            adapter = BasecampAdapter(config)
            for target, expected_id in (("chat:10:30", "71"), ("recording:10:40", "72")):
                with self.subTest(target=target):
                    direct = await adapter.send(target, "Hello")
                    self.assertTrue(direct.success)
                    self.assertEqual(direct.message_id, expected_id)
                    standalone = await _standalone_send(config, target, "Hello")
                    self.assertEqual(standalone["message_id"], expected_id)
            client.post_chat.side_effect = RuntimeError("write failed")
            self.assertFalse((await adapter.send("chat:10:30", "Fail")).success)
            self.assertIn("write failed", (await _standalone_send(config, "chat:10:30", "Fail"))["error"])
        client.post_chat.assert_any_await("10", "30", "<div>Hello</div>")
        client.post_comment.assert_any_await("10", "40", "<div>Hello</div>")
        client.verify_chat_authorship.assert_awaited_with("30", "71")
        client.verify_comment_authorship.assert_awaited_with("72")

    async def test_poll_loop_revoked_token_sets_nonretryable_fatal_state(self):
        adapter = object.__new__(BasecampAdapter)
        adapter.platform = next(iter(Platform))
        adapter._running = True
        adapter._poll_seconds = 10
        adapter._health = SimpleNamespace(
            connected=True,
            revoked=True,
            mark=MagicMock(),
        )
        adapter._poller = SimpleNamespace(health=adapter._health)
        adapter._poll_once = AsyncMock(side_effect=RuntimeError("revoked"))
        adapter._set_fatal_error = MagicMock(side_effect=lambda *args, **kwargs: setattr(adapter, "_running", False))

        await adapter._poll_loop()

        self.assertFalse(adapter._health.connected)
        adapter._set_fatal_error.assert_called_once_with("basecamp_oauth_revoked", "revoked", retryable=False)
