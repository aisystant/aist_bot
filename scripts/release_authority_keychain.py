"""Keychain-backed AuthorityVerifierPort (WP-562 Ф3.2).

Peer-session 2026-10-14-09-wp562-f32-f35-payment-migration (Claude+Kimi+Codex).

Pilot decision (escalation-00.md): single-operator model. One person signs
AND runs the promotion -- no second human, no separate macOS account, no
sandboxed daemon.

Pilot decision (escalation-01.md): that single-operator model is accepted as
an explicit, documented RESIDUAL RISK, not as a claim of real security
isolation. The contract's `independent_approver_required: true` is satisfied
here only in the narrow, literal sense the core checks for -- two DIFFERENT
identity strings, one for the human (`approver_identity`), one for the code
that runs the promotion (`runner_identity`) -- not in the sense the field
name suggests to a reader (a human approver independent of a human runner).
If the same person is compromised, this does not stop them: it only stops a
*program bug* that accidentally reuses the approver's own identity as the
runner's. Real human-level independence (separate account, sandboxed
daemon -- see session 02-peer-kimi.md, 02-peer-codex.md for both designs)
is deferred until the pilot decides it is worth the operational cost.

Residual risk (cold review, 2026-10-14): `approver_identity` is whatever
string the signer wrote into the payload before signing -- nothing here
binds a given `key_id` to one expected identity, so anyone holding any
registered private key can sign as any claimed `approver_identity` and it
will verify. Not exploitable under the single-operator model (there is only
one key, one account), but will need a `key_id -> expected identity` pin
(in the same public-keys file, or a separate one) before a second signer
ever exists.

Identity scheme:
* `approver_identity` = `f"operator.{os.getuid()}"` -- the macOS account that
  holds the signing key in Keychain.
* `runner_identity` = `f"release-runner.{sha256(release_control.py)[:16]}"`
  -- keyed by the *code* being executed, not by any account. Computed the
  same way by both the signer (when it asserts `runner_identity` inside the
  signed evidence) and the verifier (when it independently re-derives the
  value to check against the claim) -- `_runner_identity()` below is the one
  formula both sides call.

Crypto: Ed25519 (`cryptography` package, already a transitive dependency via
requirements.txt). The private key lives only in the macOS Keychain
(`security` CLI, generic password item) -- never written to a file on disk.
The public key is not a secret; it is pinned in a small JSON file next to the
contract (`release-authority-public-keys.json`) so the verifier does not need
Keychain access at all (residual-risk note above: this is why `runner_identity`
cannot be "the same Keychain", or nothing would distinguish it from
`approver_identity`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from release_manifest import canonical_sha256
from scripts.release_control import AuthorityVerificationReceipt

TRUST_ROOT_IDENTITY = "release-authority-root"
DEFAULT_KEYCHAIN_SERVICE = "iwe.release-control.authority"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_RELEASE_CONTROL_PATH = _REPO_ROOT / "scripts" / "release_control.py"
DEFAULT_PUBLIC_KEYS_PATH = (
    _REPO_ROOT / ".github" / "release-authority-public-keys.json"
)


class AuthorityKeychainError(RuntimeError):
    """Raised when Keychain access or key material is unusable."""


def _runner_identity() -> str:
    digest = hashlib.sha256(_RELEASE_CONTROL_PATH.read_bytes()).hexdigest()
    return f"release-runner.{digest[:16]}"


def _approver_identity() -> str:
    return f"operator.{os.getuid()}"


def _keychain_find(account: str, service: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True,
            check=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 44:  # item not found
            return None
        raise AuthorityKeychainError(
            f"security find-generic-password failed: {exc.stderr.strip()}"
        ) from exc
    return base64.b64decode(result.stdout.strip())


def _keychain_store(account: str, service: str, private_key_bytes: bytes) -> None:
    # Residual risk (cold review, 2026-10-14): the key is briefly visible in
    # this process's own argv (`ps`-readable by other local processes/users)
    # for the lifetime of this one `security add-generic-password` call --
    # only at key-creation time (ensure_keypair()), never during ordinary
    # sign()/verify(). Acceptable on a single-operator Mac; would need a
    # different mechanism (stdin pipe is not supported by `security`, so a
    # real fix means a different keystore) before a multi-user host.
    encoded = base64.b64encode(private_key_bytes).decode("ascii")
    try:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
                encoded,
                "-U",  # update in place if it already exists
            ],
            capture_output=True,
            check=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise AuthorityKeychainError(
            f"security add-generic-password failed: {exc.stderr.strip()}"
        ) from exc


def _load_public_keys(public_keys_path: Path) -> dict[str, bytes]:
    if not public_keys_path.exists():
        return {}
    raw = json.loads(public_keys_path.read_text())
    return {key_id: base64.b64decode(b64_value) for key_id, b64_value in raw.items()}


def _save_public_key(public_keys_path: Path, key_id: str, public_key_bytes: bytes) -> None:
    existing = _load_public_keys(public_keys_path)
    existing[key_id] = public_key_bytes
    serializable = {
        key_id: base64.b64encode(value).decode("ascii")
        for key_id, value in existing.items()
    }
    public_keys_path.write_text(json.dumps(serializable, indent=2, sort_keys=True) + "\n")


class KeychainAuthoritySigner:
    """Creates and holds the Ed25519 key an operator signs authority evidence with.

    One signer = one macOS account = one Keychain item. `account` defaults to
    the running process's own account (the single-operator model); passing a
    different value is only meaningful once a real second identity exists.
    `public_keys_path`/`keychain_service` default to the real, shared
    locations; tests pass a `tmp_path` file so a test run never touches the
    repository's pinned keys or the real login Keychain item.
    """

    def __init__(
        self,
        key_id: str,
        *,
        account: str | None = None,
        public_keys_path: Path = DEFAULT_PUBLIC_KEYS_PATH,
        keychain_service: str = DEFAULT_KEYCHAIN_SERVICE,
    ) -> None:
        self.key_id = key_id
        self.account = account or _approver_identity()
        self._public_keys_path = public_keys_path
        self._keychain_service = keychain_service

    def ensure_keypair(self) -> None:
        if _keychain_find(self.account, self._keychain_service) is not None:
            return
        private_key = Ed25519PrivateKey.generate()
        raw_private = private_key.private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )
        _keychain_store(self.account, self._keychain_service, raw_private)
        raw_public = private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        _save_public_key(self._public_keys_path, self.key_id, raw_public)

    def sign(self, signed_payload: bytes) -> str:
        raw_private = _keychain_find(self.account, self._keychain_service)
        if raw_private is None:
            raise AuthorityKeychainError(
                f"no private key in Keychain for account {self.account!r} -- "
                "call ensure_keypair() first"
            )
        private_key = Ed25519PrivateKey.from_private_bytes(raw_private)
        signature = private_key.sign(signed_payload)
        return base64.b64encode(signature).decode("ascii")

    @property
    def approver_identity(self) -> str:
        return self.account

    @property
    def runner_identity(self) -> str:
        return _runner_identity()


class KeychainAuthorityVerifier:
    """AuthorityVerifierPort implementation backed by a pinned public-key file.

    Only needs read access to `release-authority-public-keys.json` -- no
    Keychain access, so it never has a path to the private signing key even
    if it runs in the exact same process as the signer.
    """

    def __init__(
        self,
        *,
        trust_root_identity: str = TRUST_ROOT_IDENTITY,
        public_keys_path: Path = DEFAULT_PUBLIC_KEYS_PATH,
    ) -> None:
        self._trust_root_identity = trust_root_identity
        self._public_keys_path = public_keys_path

    def verify(
        self,
        *,
        key_id: str,
        signed_payload: bytes,
        signature: str,
    ) -> AuthorityVerificationReceipt:
        try:
            payload = json.loads(signed_payload)
            approver_identity = payload["approver_identity"]
            authority_id = payload["authority_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            # A malformed or incomplete payload is a caller error, not a
            # signature question -- there is nothing meaningful to report
            # signature_valid=False about yet (we have no `payload` to put
            # in the receipt). The one caller in this codebase
            # (_validate_authority, release_control.py) wraps verify() in
            # try/except Exception and rejects on any failure either way,
            # so re-raising here keeps the whole pipeline fail-closed while
            # being honest that THIS method did not produce a receipt.
            raise AuthorityKeychainError(
                f"malformed authority payload: {exc}"
            ) from exc

        public_keys = _load_public_keys(self._public_keys_path)
        raw_public = public_keys.get(key_id)
        runner_identity = _runner_identity()  # independent, not from payload
        signature_valid = False
        if raw_public is not None:
            try:
                public_key = Ed25519PublicKey.from_public_bytes(raw_public)
                public_key.verify(base64.b64decode(signature), signed_payload)
                signature_valid = True
            except (InvalidSignature, ValueError):
                signature_valid = False
        return AuthorityVerificationReceipt(
            schema_version=1,
            key_id=key_id,
            authority_id=authority_id,
            signer_identity=approver_identity,
            runner_identity=runner_identity,
            signed_payload_digest=canonical_sha256(payload),
            trust_root_identity=self._trust_root_identity,
            signature_valid=signature_valid,
            independent_signer=runner_identity != approver_identity,
            evidence_hash=canonical_sha256(
                {"key_id": key_id, "signature": signature, "payload": payload}
            ),
        )
