"""Offline tests for the Railway provider/observer stubs (WP-562 Ф3.3)."""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import Mock

import scripts.railway_release_provider as railway_release_provider
from scripts.railway_release_provider import (
    ObserverCapabilityUnavailable,
    ProviderCapabilityUnavailable,
    UnavailableRailwayProviderObserver,
    UnavailableRailwayReleaseProvider,
    release_observer_factory,
    release_provider_factory,
)

_TRANSPORT_MODULE_NAMES = ("httpx", "requests", "urllib.request", "aiohttp", "socket")


class UnavailableProviderTests(unittest.TestCase):
    def test_apply_raises_without_touching_the_request(self) -> None:
        """A buggy apply() that reads/calls something on request would fail
        this assertion (method_calls records every attribute-call on the
        mock), unlike `request.assert_not_called()`, which only checks
        whether the mock itself was invoked as `request(...)` -- something
        no plausible apply() implementation would ever do, making that
        particular assertion close to vacuous. Dropped in favor of this one.
        """
        provider = UnavailableRailwayReleaseProvider()
        request = Mock(name="ProviderMutationRequest")

        with self.assertRaises(ProviderCapabilityUnavailable):
            provider.apply(request)

        self.assertEqual(request.method_calls, [])

    def test_module_imports_no_transport_library(self) -> None:
        """`method_calls == []` only proves "no call on this one mock" --
        it says nothing about a stray network call that doesn't go through
        `request` at all. Guard the actual invariant (no transport import in
        this module) statically, so a future edit that adds one fails here
        instead of passing silently.
        """
        source = inspect.getsource(railway_release_provider)
        for name in _TRANSPORT_MODULE_NAMES:
            self.assertNotIn(
                name,
                source,
                f"railway_release_provider.py must not import {name!r} -- "
                "apply() is required to make zero network calls",
            )

    def test_factory_returns_the_stub(self) -> None:
        self.assertIsInstance(
            release_provider_factory(), UnavailableRailwayReleaseProvider
        )

    def test_factory_is_not_configurable_by_argument(self) -> None:
        signature = inspect.signature(release_provider_factory)
        self.assertEqual(len(signature.parameters), 0)


class UnavailableObserverTests(unittest.TestCase):
    def test_observe_verified_raises_without_touching_the_operation(self) -> None:
        observer = UnavailableRailwayProviderObserver()
        operation = Mock(name="ReleaseOperation")
        uncertainty_started_at = Mock(name="datetime")

        with self.assertRaises(ObserverCapabilityUnavailable):
            observer.observe_verified(operation, uncertainty_started_at)

        self.assertEqual(operation.method_calls, [])
        self.assertEqual(uncertainty_started_at.method_calls, [])

    def test_factory_returns_the_stub(self) -> None:
        self.assertIsInstance(
            release_observer_factory(), UnavailableRailwayProviderObserver
        )

    def test_factory_is_not_configurable_by_argument(self) -> None:
        signature = inspect.signature(release_observer_factory)
        self.assertEqual(len(signature.parameters), 0)

    def test_capability_errors_are_distinct_exception_types(self) -> None:
        """apply() and observe_verified() must fail with distinguishable
        errors -- a caller (or a future diagnostic) telling them apart by
        exception type should not have to parse the message text.
        """
        self.assertFalse(issubclass(ObserverCapabilityUnavailable, ProviderCapabilityUnavailable))
        self.assertFalse(issubclass(ProviderCapabilityUnavailable, ObserverCapabilityUnavailable))


if __name__ == "__main__":
    unittest.main()
