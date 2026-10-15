"""Offline tests for FileQualificationEvidence (WP-562 Ф3.4).

Same journal discipline as test_release_ledger_file.py (Ф3.1): a real
temp file on disk under a real fcntl lock, no mocking of the storage layer
itself -- these tests exercise the actual append/idempotency/echo-back
behavior, not a fake.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from scripts.release_control import QualificationRecordRequest
from scripts.release_qualification_evidence_file import FileQualificationEvidence


def _request(**overrides) -> QualificationRecordRequest:
    fields = {
        "repository": "aisystant/aist_bot",
        "contract_digest": "sha256:" + "0" * 64,
        "contract_edition": "wp562-f1-c",
        "release_id": "test-release",
        "pilot_claim_id": "11111111-1111-1111-1111-111111111111",
        "pilot_operation_fingerprint": "sha256:" + "4" * 64,
        "pilot_applied_receipt_hash": "sha256:" + "5" * 64,
        "qualification_receipt_hash": "sha256:" + "6" * 64,
        "external_receipt_hash": "sha256:" + "7" * 64,
        "artifact_digest": "sha256:" + "2" * 64,
        "embedded_manifest_hash": "sha256:" + "1" * 64,
        "verifier_identity": "pilot-qualification-resolver.v1",
        "qualification_verified_at": datetime(2026, 10, 14, 17, 0, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return QualificationRecordRequest(**fields)


class FileQualificationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="wp562-qualification-evidence-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "qualification-evidence.jsonl"
        self.store = FileQualificationEvidence(path=self.path)

    def test_record_durable_echoes_request_fields_back(self):
        request = _request()

        receipt = self.store.record_durable(request)

        self.assertEqual(receipt.request_digest, request.request_digest)
        self.assertEqual(receipt.repository, request.repository)
        self.assertEqual(receipt.pilot_claim_id, request.pilot_claim_id)
        self.assertEqual(
            receipt.qualification_receipt_hash, request.qualification_receipt_hash
        )
        self.assertEqual(
            receipt.external_receipt_hash, request.external_receipt_hash
        )
        self.assertEqual(
            receipt.qualification_verified_at, request.qualification_verified_at
        )
        self.assertTrue(receipt.durable)
        self.assertEqual(receipt.state_version, 1)
        self.assertGreaterEqual(receipt.recorded_at, request.qualification_verified_at)

    def test_record_durable_persists_across_store_instances(self):
        request = _request()
        self.store.record_durable(request)

        reopened = FileQualificationEvidence(path=self.path)
        second_request = _request(release_id="another-release")
        receipt = reopened.record_durable(second_request)

        # Second, distinct request becomes the second record in the journal
        # the first store instance started -- proves the journal, not an
        # in-memory cache, is what persists.
        self.assertEqual(receipt.state_version, 2)

    def test_repeated_call_with_same_request_is_idempotent(self):
        request = _request()

        first = self.store.record_durable(request)
        second = self.store.record_durable(request)

        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(first.recorded_at, second.recorded_at)
        self.assertEqual(first.state_version, second.state_version)
        with self.path.open(encoding="utf-8") as journal:
            line_count = sum(1 for line in journal if line.strip())
        self.assertEqual(line_count, 1, "idempotent replay must not duplicate")

    def test_distinct_requests_get_increasing_state_versions(self):
        first = self.store.record_durable(_request(release_id="release-a"))
        second = self.store.record_durable(_request(release_id="release-b"))
        third = self.store.record_durable(_request(release_id="release-c"))

        self.assertEqual([first.state_version, second.state_version, third.state_version], [1, 2, 3])

    def test_malformed_journal_line_fails_closed(self):
        self.store.record_durable(_request())
        with self.path.open("a", encoding="utf-8") as sink:
            sink.write("not json\n")

        from scripts.release_qualification_evidence_file import (
            QualificationEvidenceJournalError,
        )

        with self.assertRaises(QualificationEvidenceJournalError):
            self.store.record_durable(_request(release_id="release-b"))


if __name__ == "__main__":
    unittest.main()
