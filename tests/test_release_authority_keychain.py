from __future__ import annotations

import base64
import subprocess
import sys
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from release_manifest import canonical_json_bytes
from scripts.release_authority_keychain import (
    TRUST_ROOT_IDENTITY,
    AuthorityKeychainError,
    KeychainAuthoritySigner,
    KeychainAuthorityVerifier,
    _save_public_key,
)

_MACOS_ONLY = pytest.mark.skipif(
    sys.platform != "darwin", reason="the real Keychain only exists on macOS"
)


def _make_payload(approver_identity: str, runner_identity: str) -> bytes:
    return canonical_json_bytes(
        {
            "approver_identity": approver_identity,
            "runner_identity": runner_identity,
            "authority_id": str(uuid.uuid4()),
            "nonce": "test-nonce",
        }
    )


@_MACOS_ONLY
def test_signer_round_trip_is_valid(tmp_path):
    keys_path = tmp_path / "public-keys.json"
    signer = KeychainAuthoritySigner(
        key_id="test-round-trip",
        account="operator.test-round-trip",
        public_keys_path=keys_path,
        keychain_service="iwe.release-control.authority.test",
    )
    signer.ensure_keypair()
    try:
        payload = _make_payload(signer.approver_identity, signer.runner_identity)
        signature = signer.sign(payload)

        verifier = KeychainAuthorityVerifier(public_keys_path=keys_path)
        receipt = verifier.verify(
            key_id="test-round-trip", signed_payload=payload, signature=signature
        )

        assert receipt.signature_valid is True
        assert receipt.trust_root_identity == TRUST_ROOT_IDENTITY
        assert receipt.signer_identity == signer.approver_identity
        assert receipt.runner_identity == signer.runner_identity
        assert receipt.independent_signer is True
        assert receipt.runner_identity != receipt.signer_identity
    finally:
        _delete_test_keychain_item(signer.account, "iwe.release-control.authority.test")


@_MACOS_ONLY
def test_colliding_identities_marked_not_independent(tmp_path):
    # Construct a payload where the signed claim lies about runner_identity
    # (equal to approver_identity). The verifier must not trust that claim --
    # it re-derives runner_identity itself and the mismatch makes the
    # signature invalid over this exact payload only if the signer actually
    # signed a *different* payload; here we sign the lying payload directly
    # to prove the independent_signer flag reacts to the VERIFIER's own
    # computation, not to whatever the payload asserts.
    keys_path = tmp_path / "public-keys.json"
    signer = KeychainAuthoritySigner(
        key_id="test-lying-payload",
        account="operator.test-lying-payload",
        public_keys_path=keys_path,
        keychain_service="iwe.release-control.authority.test",
    )
    signer.ensure_keypair()
    try:
        lying_payload = _make_payload(
            approver_identity=signer.runner_identity,  # claims to equal runner
            runner_identity=signer.runner_identity,
        )
        signature = signer.sign(lying_payload)

        verifier = KeychainAuthorityVerifier(public_keys_path=keys_path)
        receipt = verifier.verify(
            key_id="test-lying-payload",
            signed_payload=lying_payload,
            signature=signature,
        )

        assert receipt.signature_valid is True  # signature itself is genuine
        assert receipt.independent_signer is False  # but identities collide
    finally:
        _delete_test_keychain_item(
            signer.account, "iwe.release-control.authority.test"
        )


def test_tampered_signature_rejected(tmp_path):
    keys_path = tmp_path / "public-keys.json"
    private_key = Ed25519PrivateKey.generate()
    public_key_bytes = private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    _save_public_key(keys_path, "fixed-key", public_key_bytes)

    payload = _make_payload("operator.fixed", "release-runner.fixed")
    genuine_signature = base64.b64encode(private_key.sign(payload)).decode("ascii")
    tampered_signature = genuine_signature[:-4] + ("A" * 4)

    verifier = KeychainAuthorityVerifier(public_keys_path=keys_path)
    receipt = verifier.verify(
        key_id="fixed-key", signed_payload=payload, signature=tampered_signature
    )

    assert receipt.signature_valid is False


def test_verifier_rejects_unknown_key_id(tmp_path):
    keys_path = tmp_path / "public-keys.json"  # never populated
    payload = _make_payload("operator.fixed", "release-runner.fixed")

    verifier = KeychainAuthorityVerifier(public_keys_path=keys_path)
    receipt = verifier.verify(
        key_id="never-registered", signed_payload=payload, signature="irrelevant"
    )

    assert receipt.signature_valid is False


def test_malformed_json_payload_raises_instead_of_silently_passing(tmp_path):
    keys_path = tmp_path / "public-keys.json"
    verifier = KeychainAuthorityVerifier(public_keys_path=keys_path)

    with pytest.raises(AuthorityKeychainError):
        verifier.verify(
            key_id="any-key", signed_payload=b"not json", signature="irrelevant"
        )


def test_payload_missing_required_field_raises_instead_of_silently_passing(tmp_path):
    keys_path = tmp_path / "public-keys.json"
    verifier = KeychainAuthorityVerifier(public_keys_path=keys_path)
    # Valid JSON, but missing "authority_id" -- the caller's own bug, not a
    # forgeable signature question.
    incomplete_payload = canonical_json_bytes({"approver_identity": "operator.x"})

    with pytest.raises(AuthorityKeychainError):
        verifier.verify(
            key_id="any-key", signed_payload=incomplete_payload, signature="irrelevant"
        )


def _delete_test_keychain_item(account: str, service: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-a", account, "-s", service],
        capture_output=True,
        check=False,
    )
