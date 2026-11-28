"""Fixture namespace and atomic-file observation must not alter product semantics."""
import asyncio
from importlib import import_module
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from homeassistant.config_entries import HANDLERS

import test_dashboard_projection as projection
import test_native_topology_migration as migration


class TestNativeFixtureIsolation(IsolatedAsyncioTestCase):
    async def test_projection_fixture_restores_registered_flow_handler(self):
        prefix = f'custom_components.{projection.DOMAIN}'
        original_module = import_module(f'{prefix}.config_flow')
        original = HANDLERS[projection.DOMAIN]
        fixture = projection.DashboardProjectionTest()
        # This nested fixture only has synchronous cleanups; initialize its
        # native unittest runner so doCleanups actually executes those callbacks.
        fixture._setupAsyncioRunner()
        try:
            await fixture.asyncSetUp()
            temporary = import_module(f'{prefix}.config_flow')
            self.assertIsNot(temporary, original_module)
            self.assertIsNot(HANDLERS[projection.DOMAIN], original)
        finally:
            self.assertTrue(fixture.doCleanups())
            fixture._tearDownAsyncioRunner()
        self.assertIs(import_module(f'{prefix}.config_flow'), original_module)
        self.assertIs(HANDLERS[projection.DOMAIN], original)

    async def test_atomic_store_observer_retries_only_transient_open_failure(self):
        import json
        expected = {'entry_id': 'fixture-entry', 'data': {'ready': True}}
        calls = 0

        def read(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError('synthetic Windows replacement sharing window')
            return json.dumps({'data': {'entries': [expected]}}).encode()

        with patch.object(Path, 'exists', return_value=True), patch.object(Path, 'read_bytes', read):
            async with asyncio.timeout(1):
                actual = await migration.disk_row(Path('fixture-only.json'), expected['entry_id'],
                                                  lambda row: row['data']['ready'])
        self.assertEqual(actual, expected)
        self.assertEqual(calls, 2)
