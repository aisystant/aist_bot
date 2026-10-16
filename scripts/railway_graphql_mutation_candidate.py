"""Unwired candidate for the Railway apply() mutation (WP-562 Ф3.3).

Peer-session 2026-10-16-03-wp562-release-control-artifact (Claude+Kimi+Codex).

Railway publishes no official schema for its GraphQL API
(`https://backboard.railway.app/graphql/v2`). This module only builds a
request and parses an ASSUMED response shape -- nobody has run it against
the live endpoint with a real token. It has no transport, no credentials,
and is not referenced by `railway_release_provider.py`'s factory: importing
this module cannot cause a network call or a production mutation.

What has to happen before this stops being a candidate (qualification,
not a vibe check):
  1. A live introspection query against the real endpoint, with a real
     Railway API token, confirming the mutation name and argument shape
     assumed below actually exist.
  2. A live dry-run of the mutation itself (ideally against a disposable
     test service, not pilot/production) confirming the response shape
     this module's parser expects.
  3. Re-running `test_railway_graphql_mutation_candidate.py` against that
     confirmed shape and bumping SCHEMA_VERSION once it matches.

QUALIFICATION_STATUS stays "unqualified" until that happens. A "stale"
status is for later: once qualified, if a live contract test (not written
yet -- needs its own live-endpoint access, same gate as qualification
itself) detects the real schema drifted from SCHEMA_VERSION.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SCHEMA_VERSION = "railway-graphql-v2-assumed-2026-10"


@dataclass(frozen=True)
class AssumedServiceSourceMutation:
    """What this candidate assumes the "switch to image" mutation looks like.

    Field and argument names are a best-effort reading of Railway's public
    docs and CLI behavior, not a confirmed schema. Do not trust these names
    without step 1 of the qualification list above.
    """

    service_id: str
    environment_id: str
    image: str

    def __post_init__(self) -> None:
        for name, value in (
            ("service_id", self.service_id),
            ("environment_id", self.environment_id),
            ("image", self.image),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if "@sha256:" not in self.image:
            raise ValueError(
                "image must be pinned by digest (name@sha256:...), not a tag"
            )


class RailwayGraphQLMutationCandidate:
    """Builds the request and parses the response for one assumed mutation.

    No transport: callers pass the response body in, this never fetches it.
    Qualification evidence (see module docstring) is a separate, later
    step -- this class cannot prove its own assumptions right.
    """

    SCHEMA_VERSION: str = SCHEMA_VERSION
    QUALIFICATION_STATUS: Literal["unqualified", "stale", "qualified"] = (
        "unqualified"
    )

    @staticmethod
    def build_request(mutation: AssumedServiceSourceMutation) -> dict[str, Any]:
        """Return the {"query": ..., "variables": ...} GraphQL request body."""
        query = """
        mutation ServiceInstanceDeployImage(
          $serviceId: String!
          $environmentId: String!
          $image: String!
        ) {
          serviceInstanceDeploy(
            serviceId: $serviceId
            environmentId: $environmentId
            source: { image: $image }
          ) {
            id
            status
          }
        }
        """
        return {
            "query": query,
            "variables": {
                "serviceId": mutation.service_id,
                "environmentId": mutation.environment_id,
                "image": mutation.image,
            },
        }

    @staticmethod
    def parse_response(response: dict[str, Any]) -> dict[str, str]:
        """Extract {"deployment_id": ..., "status": ...} from an assumed reply.

        Raises ValueError on any shape that doesn't match the assumption --
        deliberately strict, since a lenient parser here would hide exactly
        the kind of silent schema drift this candidate exists to flag.
        """
        errors = response.get("errors")
        if errors:
            raise ValueError(f"Railway GraphQL returned errors: {errors}")
        try:
            result = response["data"]["serviceInstanceDeploy"]
            deployment_id = result["id"]
            status = result["status"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"response does not match the assumed shape (schema "
                f"{SCHEMA_VERSION}): {response!r}"
            ) from exc
        if not isinstance(deployment_id, str) or not isinstance(status, str):
            raise ValueError(
                f"response fields have unexpected types (schema "
                f"{SCHEMA_VERSION}): {response!r}"
            )
        if not deployment_id.strip() or not status.strip():
            raise ValueError(
                f"response fields are empty (schema {SCHEMA_VERSION}): "
                f"{response!r}"
            )
        return {"deployment_id": deployment_id, "status": status}
