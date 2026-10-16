"""ReleaseProviderPort implementation boundary for Railway (WP-562 Ф3.3).

Peer-session 2026-10-16-03-wp562-release-control-artifact (Claude+Kimi+Codex).

The real mutation (switching a Railway service's deploy source to an image
by digest) requires a GraphQL call against a schema Railway does not publish
officially (ArchGate 2026-10-16, DRR-f33-railway-provider-sdk-choice.md).
Nobody has verified that schema against a live call yet -- doing so from
this module, blind, would either silently fail or, worse, mutate a real
service on a guess.

`UnavailableRailwayReleaseProvider` is therefore the only `ReleaseProviderPort`
implementation wired into this codebase today: `apply()` raises before any
network call, every time, unconditionally. The real GraphQL mutation shape
lives in `railway_graphql_mutation_candidate.py` as an unwired candidate --
see that module's docstring for what still has to happen before anything
here can change.
"""

from __future__ import annotations

from scripts.release_control import ProviderApplyResult, ProviderMutationRequest


class ProviderCapabilityUnavailable(RuntimeError):
    """Raised by apply() when no qualified provider backend exists yet."""


class UnavailableRailwayReleaseProvider:
    """ReleaseProviderPort stub: apply() always raises, never calls out.

    Matches the Protocol structurally (same method signature as
    ReleaseProviderPort.apply) so it can be passed anywhere a real provider
    is expected, and the caller's existing fail-closed handling (an
    exception from `provider.apply()` is already treated as an unknown
    outcome by `execute_operation`) takes over without any new branch in
    release_control.py.
    """

    def apply(self, request: ProviderMutationRequest) -> ProviderApplyResult:
        raise ProviderCapabilityUnavailable(
            "Railway provider is not qualified for apply() yet -- the "
            "GraphQL mutation candidate has not been verified against a "
            "live schema (see railway_graphql_mutation_candidate.py). No "
            "request was sent."
        )


def release_provider_factory() -> UnavailableRailwayReleaseProvider:
    """Single source of the active provider. Always the stub today.

    Deliberately not configurable by environment variable or caller
    argument: swapping to a qualified provider is its own change, made here,
    after qualification evidence exists -- not a runtime toggle that could
    be flipped without that evidence.
    """
    return UnavailableRailwayReleaseProvider()
