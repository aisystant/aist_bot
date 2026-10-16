"""Railway ReleaseProviderPort/ProviderObserverPort boundary (WP-562 Ф3.3).

Peer-session 2026-10-16-03-wp562-release-control-artifact (Claude+Kimi+Codex).

Both the real mutation (switching a Railway service's deploy source to an
image by digest, `apply()`) and real verified observation (`observe_verified()`)
are unimplemented here, on purpose, for two different but equally hard
reasons -- see each stub's own docstring. Neither stub makes any network,
CLI, or database call; both raise before touching anything external, and
both leave the caller's existing fail-closed handling (`execute_operation`'s
`except Exception` around `provider.apply()`, `reconcile_uncertain_operation`'s
around `observer.observe_verified()`) to do exactly what it already does for
any other provider failure -- no new branch needed in release_control.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from scripts.release_control import (
    ObservationEnvelope,
    ProviderApplyResult,
    ProviderMutationRequest,
    ReleaseOperation,
)


class ProviderCapabilityUnavailable(RuntimeError):
    """Raised by apply() when no qualified provider backend exists yet."""


class ObserverCapabilityUnavailable(RuntimeError):
    """Raised by observe_verified() when no qualified observer exists yet."""


class UnavailableRailwayReleaseProvider:
    """ReleaseProviderPort stub: apply() always raises, never calls out.

    The real mutation requires a GraphQL call against a schema Railway does
    not publish officially (ArchGate 2026-10-16,
    DRR-f33-railway-provider-sdk-choice.md). Nobody has verified that schema
    against a live call yet -- doing so from this module, blind, would
    either silently fail or, worse, mutate a real service on a guess. The
    assumed mutation shape lives in `railway_graphql_mutation_candidate.py`
    as an unwired candidate -- see that module's docstring for what still
    has to happen before anything here can change.
    """

    def apply(self, request: ProviderMutationRequest) -> ProviderApplyResult:
        raise ProviderCapabilityUnavailable(
            "Railway provider is not qualified for apply() yet -- the "
            "GraphQL mutation candidate has not been verified against a "
            "live schema (see railway_graphql_mutation_candidate.py). No "
            "request was sent."
        )


class UnavailableRailwayProviderObserver:
    """ProviderObserverPort stub: observe_verified() always raises.

    Here the blocker isn't an unverified schema -- it's that
    `ProviderObservation` (release_control.py) requires seven
    `observed_*` fields that would each need independent confirmation from
    THREE different sources, and `observe_verified(operation,
    uncertainty_started_at)` alone cannot reach any of them honestly:

      - Railway's own API can confirm `observed_previous_artifact_digest`
        (via `meta.imageDigest`, already live-verified readable per the
        WP-562 card, 2026-10-14) -- but nothing else on this list.
      - `observed_current_schema` / `observed_migration_history_digest` are
        facts about the bot's own application database, not something
        Railway's infrastructure API has any way to know.
      - `claim_id` / `fencing_token` / `not_before` / `not_after` were
        recorded in the ledger at claim time (`ReleaseLedgerPort.
        record_provider_intent`) -- they are not derivable from `operation`
        and this port's signature does not hand them to the observer.

    Peer-session consensus (round 6, Codex): copying any of these from
    `operation` or inventing placeholder values to satisfy the dataclass
    constructor is exactly the "fabricated field" failure mode ruled out
    for `apply()` in round 4-5 -- it would silently produce a
    `ProviderObservation` whose fields *look* confirmed but are not, which
    is worse than refusing to observe at all. A real implementation needs
    its own `ReleaseLedgerPort` dependency (to read claim data) plus a live
    database-read capability (to confirm schema) -- both shown earlier in
    this same session to need a human in the loop for a live check. That is
    a separate, larger design, not something to improvise here.

    Raising here is not a weaker behavior than "no observer at all": the
    caller (`reconcile_uncertain_operation`) already treats any observer
    exception as fail-closed -- the operation simply stays `OUTCOME_UNKNOWN`
    pending manual review, same as if this class didn't exist.
    """

    def observe_verified(
        self,
        operation: ReleaseOperation,
        uncertainty_started_at: datetime,
    ) -> Sequence[ObservationEnvelope]:
        raise ObserverCapabilityUnavailable(
            "Railway observer is not qualified for observe_verified() yet "
            "-- verified ProviderObservation requires claim data this port "
            "signature does not receive and a live database schema check "
            "neither Railway's API nor this method can perform. No "
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


def release_observer_factory() -> UnavailableRailwayProviderObserver:
    """Single source of the active observer. Always the stub today.

    Same non-configurability rule as release_provider_factory() above, for
    the same reason: there is no qualification evidence yet to make a real
    observer safe to return.
    """
    return UnavailableRailwayProviderObserver()
