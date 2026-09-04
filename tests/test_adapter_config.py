import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.secret_scope import UnscopedSecretError, set_multiplex_active

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "basecamp_plugin",
    ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
assert SPEC and SPEC.loader
PLUGIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)

from basecamp_plugin.adapter import (
    _RUNTIME_HEALTH,
    _apply_yaml_config,
    _backoff_after_failure,
    _parse_target_ref,
    _secret,
    _settings,
    _validate_target_ref,
    is_connected,
    validate_config,
)
from basecamp_plugin.health import RuntimeHealth


class AdapterConfigTests(unittest.TestCase):
    def test_secret_resolution_fails_closed_when_multiplex_scope_is_missing(self):
        set_multiplex_active(True)
        try:
            with (
                patch.dict(os.environ, {"BASECAMP_ACCOUNT_ID": "wrong-profile"}),
                self.assertRaises(UnscopedSecretError),
            ):
                _secret("BASECAMP_ACCOUNT_ID")
        finally:
            set_multiplex_active(False)

    def test_plugin_registration_does_not_read_profile_secrets(self):
        class Context:
            def register_platform(self, **kwargs):
                self.platform = kwargs

            def register_tool(self, **kwargs):
                pass

            def register_hook(self, name, callback):
                pass

        from basecamp_plugin.adapter import register

        set_multiplex_active(True)
        try:
            register(Context())
        finally:
            set_multiplex_active(False)

    def test_parses_prefixed_recording_target(self):
        self.assertEqual(
            _parse_target_ref("basecamp:bucket:11111111/recording:22222222"),
            ("bucket:11111111/recording:22222222", None),
        )

    def test_rejects_ambiguous_target(self):
        self.assertIsNone(_parse_target_ref("Example project"))

    @patch.dict(os.environ, {"BASECAMP_PROJECT_IDS": "11111111"}, clear=False)
    def test_validator_enforces_project_allowlist(self):
        self.assertTrue(_validate_target_ref("bucket:11111111/recording:22222222"))
        self.assertIn("allowlist", _validate_target_ref("bucket:999/recording:22222222"))

    def test_yaml_mapping_keeps_tokens_out_of_yaml_contract(self):
        extras = _apply_yaml_config(
            {},
            {
                "account_id": 1234567,
                "person_id": 7654321,
                "person_email": "agent@example.com",
                "projects": [11111111],
                "token_path": "/private/agent.json",
                "poll_chat_seconds": 15,
            },
        )
        self.assertEqual(extras["project_ids"], "11111111")
        self.assertEqual(extras["token_file"], "/private/agent.json")
        self.assertNotIn("access_token", extras)

    def test_settings_accepts_project_id_list_from_gateway_yaml(self):
        config = SimpleNamespace(extra={"project_ids": [11111111]})

        self.assertEqual(_settings(config)["project_ids"], ("11111111",))

    def test_settings_accepts_null_optional_expiry_from_gateway_yaml(self):
        config = SimpleNamespace(extra={"expires_at": None})

        self.assertIsNone(_settings(config)["expires_at"])

    def test_is_connected_uses_live_attested_runtime_health(self):
        config = SimpleNamespace(
            extra={
                "account_id": "1",
                "person_id": "2",
                "person_email": "agent@example.com",
                "mention": "@agent",
                "project_ids": ["10"],
                "access_token": "test",
            }
        )
        health = RuntimeHealth(connected=True, identity_ok=True, role_ok=True)
        _RUNTIME_HEALTH[("1", "2")] = health
        self.assertTrue(is_connected(config))
        health.revoked = True
        self.assertFalse(is_connected(config))

    def test_poll_failure_backoff_is_exponential_and_capped(self):
        self.assertEqual(_backoff_after_failure(10, 10), 20)
        self.assertEqual(_backoff_after_failure(20, 10), 40)
        self.assertEqual(_backoff_after_failure(200, 10), 300)

    @patch.dict(os.environ, {"BASECAMP_WEBHOOK_TLS_PROXY": "true"}, clear=False)
    def test_explicit_yaml_false_cannot_inherit_environment_tls_assertion(self):
        config = SimpleNamespace(
            extra={
                "account_id": "1",
                "person_id": "2",
                "person_email": "agent@example.com",
                "mention": "@agent",
                "project_ids": ["10"],
                "access_token": "test",
                "webhook_host": "0.0.0.0",
                "webhook_port": 8788,
                "webhook_tls_proxy": False,
            }
        )
        self.assertFalse(_settings(config)["webhook_tls_proxy"])
        self.assertFalse(validate_config(config))
