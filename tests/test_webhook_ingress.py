import asyncio
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from webhook_ingress import (
    DurableWebhookStore,
    WebhookHTTPReceiver,
    WebhookIngress,
    WebhookRejectedError,
)


class _Client:
    project_ids = ("10",)

    async def fetch_webhook_event(self, payload):
        if payload.get("poison"):
            raise RuntimeError("canonical failure")
        return {
            "id": payload["id"],
            "kind": "comment_created",
            "created_at": "2026-09-04T01:00:00Z",
            "creator": {"id": 8, "name": "Canonical"},
            "bucket": {"id": payload.get("canonical_project", 10)},
            "recording": {"id": 2, "type": "Comment", "content": "canonical"},
            "parent": {"id": 3},
        }


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_canonical_scope_and_duplicate_checks(self):
        ingress = WebhookIngress(_Client(), token="t" * 32)
        with self.assertRaises(WebhookRejectedError):
            await ingress.ingest("wrong" * 8, {"id": 1})
        with self.assertRaises(WebhookRejectedError):
            await ingress.ingest("t" * 32, {"id": 2, "canonical_project": 99})
        self.assertTrue(await ingress.ingest("t" * 32, {"id": 3}))
        self.assertFalse(await ingress.ingest("t" * 32, {"id": 3}))

    async def test_forged_actor_summary_and_mention_never_enter_dispatch_queue(self):
        ingress = WebhookIngress(_Client(), token="t" * 32)
        self.assertTrue(
            await ingress.ingest(
                "t" * 32,
                {
                    "id": 12,
                    "creator": {"id": 999, "name": "Forged"},
                    "summary": '<bc-attachment content-type="application/vnd.basecamp.mention" '
                    'sgid="sgid://bc3/Person/7"></bc-attachment>',
                    "recording": {"id": 999, "type": "Comment", "content": "forged mention"},
                },
            )
        )
        queued = await ingress.get()
        self.assertEqual(queued["creator"], {"id": 8, "name": "Canonical"})
        self.assertEqual(queued["recording"]["content"], "canonical")
        self.assertNotIn("summary", queued)

    async def test_durable_delivery_recovers_after_restart_and_acks_once(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.sqlite3"
            first = WebhookIngress(_Client(), token="t" * 32, store=DurableWebhookStore(path))
            await first.ingest("t" * 32, {"id": 4})
            claimed = await first.get()
            second = WebhookIngress(_Client(), token="t" * 32, store=DurableWebhookStore(path))
            recovered = await second.get()
            self.assertEqual(recovered["id"], 4)
            second.ack(recovered)
            self.assertFalse(await second.ingest("t" * 32, {"id": 4}))
            self.assertEqual(claimed["id"], 4)

    async def test_poison_event_does_not_enter_queue(self):
        ingress = WebhookIngress(_Client(), token="t" * 32)
        with self.assertRaisesRegex(RuntimeError, "canonical failure"):
            await ingress.ingest("t" * 32, {"id": 5, "poison": True})
        self.assertTrue(ingress.queue.empty())

    async def test_burst_over_bound_requests_retry_without_dropping_first_event(self):
        ingress = WebhookIngress(_Client(), token="t" * 32, max_queue=1)
        await ingress.ingest("t" * 32, {"id": 6})
        with self.assertRaisesRegex(RuntimeError, "queue is full"):
            await ingress.ingest("t" * 32, {"id": 7})
        self.assertEqual((await ingress.get())["id"], 6)

    async def test_durable_drain_leases_only_one_event(self):
        with tempfile.TemporaryDirectory() as temp:
            ingress = WebhookIngress(
                _Client(), token="t" * 32, store=DurableWebhookStore(Path(temp) / "events.sqlite3")
            )
            await ingress.ingest("t" * 32, {"id": 10})
            await ingress.ingest("t" * 32, {"id": 11})
            first = await ingress.drain_nowait()
            self.assertEqual(len(first), 1)
            self.assertEqual(len(await ingress.drain_nowait()), 1)

    async def test_cancelled_claim_returns_event_to_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DurableWebhookStore(Path(temp) / "events.sqlite3")
            ingress = WebhookIngress(_Client(), token="t" * 32, store=store)
            await ingress.ingest("t" * 32, {"id": 13})
            original_next = store.next
            started = threading.Event()
            release = threading.Event()

            def blocked_next():
                started.set()
                release.wait(timeout=2)
                return original_next()

            with patch.object(store, "next", side_effect=blocked_next):
                task = asyncio.create_task(ingress.drain_nowait())
                await asyncio.to_thread(started.wait, 2)
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            recovered = await ingress.drain_nowait()
            self.assertEqual(recovered[0]["id"], 13)

    def test_store_closes_sqlite_connections(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DurableWebhookStore(Path(temp) / "events.sqlite3")
            connection = MagicMock()
            with patch.object(store, "_connect", return_value=connection):
                store.ack("1")
            connection.close.assert_called_once_with()

    def test_store_closes_connection_when_pragma_setup_fails(self):
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError("pragma failed")
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("webhook_ingress.sqlite3.connect", return_value=connection),
            self.assertRaises(sqlite3.OperationalError),
        ):
            DurableWebhookStore(Path(temp) / "events.sqlite3")
        connection.close.assert_called_once_with()

    async def test_http_receiver_checks_token_body_limit_and_lifecycle(self):
        ingress = WebhookIngress(_Client(), token="t" * 32)
        receiver = WebhookHTTPReceiver(ingress, token="t" * 32, port=0, max_body_bytes=32)
        await receiver.start()
        self.assertTrue(receiver.running)
        self.assertGreater(receiver.port, 0)
        denied = await receiver.handle("POST", "/basecamp/webhooks/wrong", b'{"id":8}')
        self.assertEqual(denied.status, 404)
        oversized = await receiver.handle("POST", receiver.path, b"x" * 33)
        self.assertEqual(oversized.status, 413)
        wrong_type = await receiver.handle("POST", receiver.path, b'{"id":8}', "text/plain")
        self.assertEqual(wrong_type.status, 415)
        accepted = await receiver.handle("POST", receiver.path, b'{"id":8}')
        self.assertEqual(accepted.status, 202)
        self.assertEqual((await ingress.get())["id"], 8)
        await receiver.stop()
        self.assertFalse(receiver.running)

    async def test_real_socket_path_accepts_and_queues_canonical_event(self):
        ingress = WebhookIngress(_Client(), token="t" * 32)
        receiver = WebhookHTTPReceiver(ingress, token="t" * 32, port=0)
        await receiver.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", receiver.port)
            body = b'{"id":81}'
            writer.write(
                f"POST {receiver.path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            self.assertIn(b"HTTP/1.1 202 Accepted", response)
            self.assertEqual((await ingress.get())["creator"]["name"], "Canonical")
        finally:
            await receiver.stop()

    async def test_terminal_rows_and_memory_dedupe_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DurableWebhookStore(
                Path(temp) / "events.sqlite3",
                max_terminal_events=2,
                terminal_retention_seconds=10_000,
            )
            ingress = WebhookIngress(_Client(), token="t" * 32, max_queue=1, store=store)
            ingress._max_seen = 2
            for event_id in range(1, 5):
                await ingress.ingest("t" * 32, {"id": event_id})
                item = await ingress.get()
                ingress.ack(item)
            with sqlite3.connect(store.path) as connection:
                terminal_count = connection.execute(
                    "SELECT COUNT(*) FROM webhook_events WHERE state IN ('done', 'poison')"
                ).fetchone()[0]
            self.assertLessEqual(terminal_count, 2)
            self.assertLessEqual(len(ingress._seen), 2)

    async def test_http_rejects_token_before_parsing_json(self):
        ingress = WebhookIngress(_Client(), token="t" * 32)
        receiver = WebhookHTTPReceiver(ingress, token="t" * 32)
        result = await receiver.handle("POST", "/basecamp/webhooks/wrong", b"not json")
        self.assertEqual(result.status, 404)

    async def test_http_maps_transient_refetch_failure_to_retry(self):
        ingress = WebhookIngress(_Client(), token="t" * 32)
        receiver = WebhookHTTPReceiver(ingress, token="t" * 32)
        result = await receiver.handle("POST", receiver.path, b'{"id":9,"poison":true}')
        self.assertEqual(result.status, 503)

    def test_non_loopback_bind_requires_explicit_tls_proxy(self):
        ingress = WebhookIngress(_Client(), token="t" * 32)
        with self.assertRaisesRegex(ValueError, "loopback"):
            WebhookHTTPReceiver(ingress, token="t" * 32, host="0.0.0.0")
        WebhookHTTPReceiver(ingress, token="t" * 32, host="0.0.0.0", tls_proxy=True)
