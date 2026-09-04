import unittest

from policy import RiskClass, default_registry
from sdk_routes import SDK_ROUTES
from tools import TOOL_SCHEMAS, _resolve_api_route


class SDKRouteTests(unittest.TestCase):
    def test_every_public_policy_capability_has_a_frozen_official_route(self):
        expected = {
            item.name for item in default_registry().list()
            if item.risk is not RiskClass.ADMINLAND_DENIED
        }
        actual = {capability for _, _, capability in SDK_ROUTES}
        self.assertEqual(actual, expected)

    def test_official_routes_resolve_without_invented_bucket_prefixes(self):
        self.assertEqual(
            _resolve_api_route("GET", "/todos/42"),
            ("todos.get", "", {"todo_id": 42}, ()),
        )
        self.assertEqual(
            _resolve_api_route("POST", "/todolists/7/todos.json"),
            ("todos.create", "", {"todolist_id": 7}, ()),
        )
        with self.assertRaises(PermissionError):
            _resolve_api_route("GET", "/buckets/10/todos/42.json")

    def test_generic_tools_require_explicit_project_scope(self):
        self.assertIn("bucket_id", TOOL_SCHEMAS["basecamp_api_read"]["required"])
        self.assertIn("bucket_id", TOOL_SCHEMAS["basecamp_api_write"]["required"])

    def test_foreign_hosts_and_query_strings_fail_closed(self):
        for path in ("https://example.com/todos/1", "/todos/1?x=1", "/../todos/1"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                _resolve_api_route("GET", path)
