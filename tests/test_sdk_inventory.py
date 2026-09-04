import inspect
import unittest

from basecamp import AsyncClient
from basecamp.async_auth import AsyncStaticTokenProvider

from policy import RiskClass, default_registry
from sdk_inventory import (
    EXCLUDED_SERVICES,
    INTERNAL_METHODS,
    SDK_016_PUBLIC_METHOD_DIGEST,
    disposition,
    inventory_digest,
    public_methods,
)


class SDKInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_non_admin_capability_exists_in_pinned_sdk(self):
        client = AsyncClient(token_provider=AsyncStaticTokenProvider("synthetic-test-token"))
        try:
            account = client.for_account("1")
            missing = []
            for capability in default_registry().list():
                if capability.risk is RiskClass.ADMINLAND_DENIED:
                    continue
                service = getattr(account, capability.service, None)
                if service is None or not callable(getattr(service, capability.method, None)):
                    missing.append(f"{capability.service}.{capability.method}")
            self.assertEqual(missing, [])
        finally:
            await client.close()

    async def test_every_sdk_public_method_has_frozen_explicit_disposition(self):
        client = AsyncClient(token_provider=AsyncStaticTokenProvider("synthetic-test-token"))
        try:
            account = client.for_account("1")
            methods = public_methods(account)
            self.assertEqual(inventory_digest(methods), SDK_016_PUBLIC_METHOD_DIGEST)
            registered = {
                f"{item.service}.{item.method}"
                for item in default_registry().list()
                if item.risk is not RiskClass.ADMINLAND_DENIED
            }
            self.assertTrue(registered.isdisjoint(INTERNAL_METHODS))
            dispositions = {method: disposition(method, registered) for method in methods}
            self.assertEqual(
                [method for method, (kind, _) in dispositions.items() if kind == "unclassified"],
                [],
            )
            self.assertTrue(
                all(kind in {"registered", "internal", "excluded"} and reason for kind, reason in dispositions.values())
            )
            self.assertFalse(any("deferred" in reason for _, reason in dispositions.values()))
            self.assertEqual(EXCLUDED_SERVICES, {"account", "http", "templates"})
        finally:
            await client.close()

    async def test_registry_argument_metadata_matches_sdk_signatures(self):
        client = AsyncClient(token_provider=AsyncStaticTokenProvider("synthetic-test-token"))
        try:
            account = client.for_account("1")
            failures = []
            for capability in default_registry().list():
                if capability.risk is RiskClass.ADMINLAND_DENIED:
                    continue
                method = getattr(getattr(account, capability.service), capability.method)
                operation_parameters = set(inspect.signature(method).parameters)
                for argument in (capability.project_argument, capability.owner_argument):
                    if (
                        argument
                        and argument not in capability.context_arguments
                        and argument not in operation_parameters
                    ):
                        failures.append(f"{capability.name}: {argument} missing from operation signature")
                for argument in capability.context_arguments:
                    if argument in operation_parameters:
                        failures.append(f"{capability.name}: context-only {argument} is an SDK argument")
                if capability.owner_service and capability.owner_method and capability.owner_argument:
                    owner = getattr(
                        getattr(account, capability.owner_service),
                        capability.owner_method,
                    )
                    owner_parameter = capability.owner_parameter or capability.owner_argument
                    if owner_parameter not in inspect.signature(owner).parameters:
                        failures.append(f"{capability.name}: {owner_parameter} missing from owner signature")
                if not capability.result_id_field:
                    failures.append(f"{capability.name}: empty verifier result ID field")
            self.assertEqual(failures, [])
        finally:
            await client.close()
