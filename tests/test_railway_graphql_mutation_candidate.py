"""Offline tests for RailwayGraphQLMutationCandidate (WP-562 Ф3.3).

No network, no transport. Every "response" below is a value this test
supplies directly -- the candidate has no way to fetch one itself.
"""

from __future__ import annotations

import unittest

from scripts.railway_graphql_mutation_candidate import (
    AssumedServiceSourceMutation,
    RailwayGraphQLMutationCandidate,
)


class AssumedMutationTests(unittest.TestCase):
    def test_rejects_image_without_digest(self) -> None:
        with self.assertRaises(ValueError):
            AssumedServiceSourceMutation(
                service_id="svc-1",
                environment_id="env-1",
                image="ghcr.io/aisystant/aist_bot-prod:latest",
            )

    def test_rejects_empty_identifiers(self) -> None:
        with self.assertRaises(ValueError):
            AssumedServiceSourceMutation(
                service_id="",
                environment_id="env-1",
                image="ghcr.io/aisystant/aist_bot-prod@sha256:" + "0" * 64,
            )

    def test_accepts_digest_pinned_image(self) -> None:
        mutation = AssumedServiceSourceMutation(
            service_id="svc-1",
            environment_id="env-1",
            image="ghcr.io/aisystant/aist_bot-prod@sha256:" + "0" * 64,
        )
        self.assertEqual(mutation.service_id, "svc-1")


class BuildRequestTests(unittest.TestCase):
    def test_build_request_embeds_all_variables(self) -> None:
        mutation = AssumedServiceSourceMutation(
            service_id="svc-1",
            environment_id="env-1",
            image="ghcr.io/aisystant/aist_bot-prod@sha256:" + "1" * 64,
        )
        request = RailwayGraphQLMutationCandidate.build_request(mutation)

        self.assertIn("serviceInstanceDeploy", request["query"])
        self.assertEqual(
            request["variables"],
            {
                "serviceId": "svc-1",
                "environmentId": "env-1",
                "image": "ghcr.io/aisystant/aist_bot-prod@sha256:" + "1" * 64,
            },
        )


class ParseResponseTests(unittest.TestCase):
    def test_parses_the_assumed_success_shape(self) -> None:
        response = {
            "data": {
                "serviceInstanceDeploy": {"id": "deploy-123", "status": "QUEUED"}
            }
        }
        parsed = RailwayGraphQLMutationCandidate.parse_response(response)
        self.assertEqual(
            parsed, {"deployment_id": "deploy-123", "status": "QUEUED"}
        )

    def test_rejects_graphql_errors(self) -> None:
        response = {"errors": [{"message": "service not found"}]}
        with self.assertRaises(ValueError):
            RailwayGraphQLMutationCandidate.parse_response(response)

    def test_rejects_missing_data_key(self) -> None:
        with self.assertRaises(ValueError):
            RailwayGraphQLMutationCandidate.parse_response({})

    def test_rejects_unexpected_mutation_name(self) -> None:
        response = {"data": {"somethingElse": {"id": "x", "status": "y"}}}
        with self.assertRaises(ValueError):
            RailwayGraphQLMutationCandidate.parse_response(response)

    def test_rejects_wrong_field_types(self) -> None:
        response = {
            "data": {"serviceInstanceDeploy": {"id": 123, "status": "QUEUED"}}
        }
        with self.assertRaises(ValueError):
            RailwayGraphQLMutationCandidate.parse_response(response)

    def test_rejects_empty_deployment_id(self) -> None:
        response = {
            "data": {"serviceInstanceDeploy": {"id": "", "status": "QUEUED"}}
        }
        with self.assertRaises(ValueError):
            RailwayGraphQLMutationCandidate.parse_response(response)

    def test_rejects_empty_status(self) -> None:
        response = {
            "data": {"serviceInstanceDeploy": {"id": "deploy-123", "status": ""}}
        }
        with self.assertRaises(ValueError):
            RailwayGraphQLMutationCandidate.parse_response(response)


class QualificationStatusTests(unittest.TestCase):
    def test_starts_unqualified(self) -> None:
        self.assertEqual(
            RailwayGraphQLMutationCandidate.QUALIFICATION_STATUS, "unqualified"
        )


if __name__ == "__main__":
    unittest.main()
