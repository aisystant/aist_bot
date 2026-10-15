#!/usr/bin/env python3
"""Generate the deterministic pre-build manifest packaged into one image."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

for _STDLIB_MODULE in ("dataclasses", "datetime", "enum", "hashlib", "typing", "uuid"):
    importlib.import_module(_STDLIB_MODULE)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RELEASE_MANIFEST_PATH = _REPO_ROOT / "release_manifest.py"
_RELEASE_CONTROL_PATH = _REPO_ROOT / "scripts" / "release_control.py"


def _load_exact_module(module_name: str, path: Path) -> object:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ImportError("trusted release-control module is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ImportError("trusted release-control module is not a regular file")
    existing = sys.modules.get(module_name)
    if existing is not None:
        origin = getattr(existing, "__file__", None)
        if origin is None or Path(origin).resolve() != path.resolve():
            raise ImportError("ambiguous release-control module is forbidden")
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("trusted release-control module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


_release_manifest = _load_exact_module("release_manifest", _RELEASE_MANIFEST_PATH)
_release_control = _load_exact_module("_wp562_release_control", _RELEASE_CONTROL_PATH)
EmbeddedManifest = _release_manifest.EmbeddedManifest
ReleaseManifestError = _release_manifest.ReleaseManifestError
canonical_json_bytes = _release_manifest.canonical_json_bytes
loads_strict_json = _release_manifest.loads_strict_json
validate_migration_semantics = _release_manifest.validate_migration_semantics
ContractError = _release_control.ContractError
validate_release_control_contract = _release_control.validate_release_control_contract

LOGGER = logging.getLogger(__name__)
_FULL_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_OUTPUT_NAME = re.compile(r"release-manifest\.[a-z0-9][a-z0-9.-]{0,47}\.json\Z")
_CONTRACT_PATH = Path(".github/release-control-contract.json")
_METADATA_PATH = Path(".github/release-metadata.json")
_MAX_CONTRACT_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 4 * 1024
_CONTEXT_NAME = "wp562-build-context"


class ManifestBuildError(RuntimeError):
    """Trusted build evidence is missing or cannot be written safely."""


@dataclass(frozen=True)
class BuildTreeBlob:
    path: str
    mode: str
    object_oid: str


def _git_oid(repository: Path, revision: str) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "-C",
                str(repository),
                "rev-parse",
                "--verify",
                "--end-of-options",
                revision,
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestBuildError("Git build evidence is unavailable") from exc
    try:
        oid = result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ManifestBuildError("Git returned non-ASCII build evidence") from exc
    if result.returncode != 0 or _FULL_OID.fullmatch(oid) is None:
        raise ManifestBuildError("Git returned invalid build evidence")
    return oid


def _committed_input(
    repository: Path,
    requested_path: str | Path,
    expected_relative_path: Path,
    commit: str,
    label: str,
    max_bytes: int,
) -> bytes:
    """Read one bounded fixed-path blob directly from the immutable commit."""

    requested = Path(requested_path)
    if not requested.is_absolute():
        requested = repository / requested
    expected = repository / expected_relative_path
    if Path(os.path.abspath(requested)) != expected:
        raise ManifestBuildError(f"only the repository {label} is permitted")
    object_spec = f"{commit}:{expected_relative_path.as_posix()}"
    try:
        size_result = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "-C",
                str(repository),
                "cat-file",
                "-s",
                object_spec,
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestBuildError(f"Git {label} evidence is unavailable") from exc
    try:
        size = int(size_result.stdout.decode("ascii", errors="strict").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestBuildError(f"Git returned invalid {label} size") from exc
    if size_result.returncode != 0 or not 0 < size <= max_bytes:
        raise ManifestBuildError(f"committed {label} is absent or oversized")
    try:
        blob_result = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "-C",
                str(repository),
                "cat-file",
                "blob",
                object_spec,
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestBuildError(f"Git {label} evidence is unavailable") from exc
    if blob_result.returncode != 0 or len(blob_result.stdout) != size:
        raise ManifestBuildError(f"Git returned invalid {label} evidence")
    return blob_result.stdout


def _tree_blobs(repository: Path, commit: str) -> tuple[BuildTreeBlob, ...]:
    try:
        result = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "-C",
                str(repository),
                "ls-tree",
                "-rz",
                "--full-tree",
                "-r",
                commit,
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestBuildError("Git build-tree evidence is unavailable") from exc
    if result.returncode != 0:
        raise ManifestBuildError("Git rejected build-tree evidence")

    entries: list[BuildTreeBlob] = []
    seen: set[str] = set()
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_oid = metadata.decode(
                "ascii", errors="strict"
            ).split(" ")
            path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ManifestBuildError("Git build tree is malformed") from exc
        parsed = PurePosixPath(path)
        if (
            not path
            or path in seen
            or parsed.is_absolute()
            or "." in parsed.parts
            or ".." in parsed.parts
            or ".git" in parsed.parts
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or _FULL_OID.fullmatch(object_oid) is None
        ):
            raise ManifestBuildError("Git build tree contains an unsafe path")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ManifestBuildError(
                "Git build tree contains a symlink, gitlink, or unsupported mode"
            )
        seen.add(path)
        entries.append(BuildTreeBlob(path, mode, object_oid))
    return tuple(entries)


def _blob_bytes(repository: Path, entry: BuildTreeBlob) -> bytes:
    try:
        result = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "-C",
                str(repository),
                "cat-file",
                "blob",
                entry.object_oid,
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestBuildError("Git build blob is unavailable") from exc
    if result.returncode != 0:
        raise ManifestBuildError("Git rejected a committed build blob")
    return result.stdout


def _worktree_blob_oid(repository: Path, path: Path) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "-C",
                str(repository),
                "hash-object",
                "--no-filters",
                str(path),
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestBuildError(
            "materialized blob verification is unavailable"
        ) from exc
    try:
        object_oid = result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ManifestBuildError("materialized blob verification is malformed") from exc
    if result.returncode != 0 or _FULL_OID.fullmatch(object_oid) is None:
        raise ManifestBuildError("materialized blob verification failed")
    return object_oid


def _safe_context_output(repository: Path, output: str | Path) -> Path:
    candidate = Path(output)
    if not candidate.is_absolute():
        candidate = repository / candidate
    expected = repository / _CONTEXT_NAME
    if Path(os.path.abspath(candidate)) != expected:
        raise ManifestBuildError(
            "build context must use its fixed repository-root path"
        )
    if candidate.is_symlink() or candidate.exists():
        raise ManifestBuildError("build context output must not already exist")
    return candidate


def materialize_build_context(
    *,
    repository: str | Path,
    source_ref: str,
    output: str | Path = _CONTEXT_NAME,
) -> tuple[str, str]:
    """Materialize exact committed blobs without archive export transformations."""

    repo = Path(repository).resolve(strict=True)
    if not repo.is_dir():
        raise ManifestBuildError("repository is unavailable")
    commit = _git_oid(repo, f"{source_ref}^{{commit}}")
    tree = _git_oid(repo, f"{commit}^{{tree}}")
    entries = _tree_blobs(repo, commit)
    destination = _safe_context_output(repo, output)
    temporary = Path(tempfile.mkdtemp(dir=repo, prefix=f".{_CONTEXT_NAME}-"))
    try:
        materialized_paths: set[str] = set()
        for entry in entries:
            relative = PurePosixPath(entry.path)
            target = temporary.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with target.open("xb") as stream:
                    stream.write(_blob_bytes(repo, entry))
                os.chmod(target, 0o755 if entry.mode == "100755" else 0o644)
            except OSError as exc:
                raise ManifestBuildError(
                    "committed build blob could not be written"
                ) from exc
            metadata = target.lstat()
            expected_permissions = 0o755 if entry.mode == "100755" else 0o644
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != expected_permissions
                or _worktree_blob_oid(repo, target) != entry.object_oid
            ):
                raise ManifestBuildError(
                    "materialized build blob differs from the immutable Git tree"
                )
            materialized_paths.add(entry.path)

        observed_paths = {
            path.relative_to(temporary).as_posix()
            for path in temporary.rglob("*")
            if path.is_file()
        }
        if observed_paths != materialized_paths:
            raise ManifestBuildError(
                "materialized build context has missing or extra blobs"
            )
        if destination.exists() or destination.is_symlink():
            raise ManifestBuildError(
                "build context output appeared during materialization"
            )
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return commit, tree


def _load_release_metadata(raw: bytes) -> dict[str, object]:
    """Load the small, versioned migration declaration without free-form data."""

    if len(raw) > _MAX_METADATA_BYTES:
        raise ManifestBuildError("release metadata exceeds the size limit")
    try:
        payload = loads_strict_json(raw)
    except ReleaseManifestError as exc:
        raise ManifestBuildError("release metadata is malformed") from exc
    expected_fields = {
        "schema_version",
        "declaration_revision",
        "migration_class",
        "schema_min",
        "schema_max",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise ManifestBuildError("release metadata has missing or extra fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ManifestBuildError("release metadata schema_version must equal 1")
    revision = payload["declaration_revision"]
    if type(revision) is not int or not 1 <= revision <= 2_147_483_647:
        raise ManifestBuildError("release metadata revision is outside its range")
    try:
        validate_migration_semantics(
            payload["migration_class"],
            payload["schema_min"],
            payload["schema_max"],
            path="release_metadata",
        )
    except ReleaseManifestError as exc:
        raise ManifestBuildError(
            "release metadata migration semantics are invalid"
        ) from exc
    return payload


def _safe_output(repository: Path, output: str | Path) -> Path:
    candidate = Path(output)
    if not candidate.is_absolute():
        candidate = repository / candidate
    if _OUTPUT_NAME.fullmatch(candidate.name) is None:
        raise ManifestBuildError("manifest output name is not permitted")
    if candidate.is_symlink() or candidate.parent.resolve() != repository:
        raise ManifestBuildError(
            "manifest output must be a regular repository-root file"
        )
    if candidate.exists() and not candidate.is_file():
        raise ManifestBuildError("manifest output is not a regular file")
    try:
        tracked = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "-C",
                str(repository),
                "ls-files",
                "--error-unmatch",
                "--",
                candidate.name,
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestBuildError("output Git ownership check is unavailable") from exc
    if tracked.returncode == 0:
        raise ManifestBuildError("manifest output must not replace a tracked file")
    if tracked.returncode != 1:
        raise ManifestBuildError("output Git ownership check failed")
    return candidate


def _write_atomic(output: Path, payload: bytes) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=".release-manifest-",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.write(b"\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, output)
        temporary_name = None
    except OSError as exc:
        raise ManifestBuildError("manifest output could not be committed") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError as cleanup_error:
                LOGGER.warning(
                    "Temporary release manifest cleanup failed (%s)",
                    type(cleanup_error).__name__,
                )


def build_release_manifest(
    *,
    repository: str | Path,
    contract_path: str | Path,
    metadata_path: str | Path,
    source_ref: str,
    release_id: str,
    output: str | Path,
) -> EmbeddedManifest:
    """Bind one source tree and the validated contract into pre-build facts."""

    repo = Path(repository).resolve(strict=True)
    if not repo.is_dir():
        raise ManifestBuildError("repository is unavailable")
    commit = _git_oid(repo, f"{source_ref}^{{commit}}")
    contract_bytes = _committed_input(
        repo,
        contract_path,
        _CONTRACT_PATH,
        commit,
        "release contract",
        _MAX_CONTRACT_BYTES,
    )
    metadata_bytes = _committed_input(
        repo,
        metadata_path,
        _METADATA_PATH,
        commit,
        "release metadata",
        _MAX_METADATA_BYTES,
    )
    contract = validate_release_control_contract(loads_strict_json(contract_bytes))
    metadata = _load_release_metadata(metadata_bytes)
    tree = _git_oid(repo, f"{commit}^{{tree}}")
    manifest = EmbeddedManifest.from_mapping(
        {
            "schema_version": 1,
            "release_id": release_id,
            "source_commit": commit,
            "source_tree": tree,
            "build_contract_digest": contract.contract_digest,
            "migration_class": metadata["migration_class"],
            "schema_min": metadata["schema_min"],
            "schema_max": metadata["schema_max"],
            "required_checks": list(contract.required_checks),
            "platform": {"os": "linux", "architecture": "amd64"},
        }
    )
    _write_atomic(
        _safe_output(repo, output), canonical_json_bytes(manifest.to_mapping())
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--source-ref", required=True, help="Full candidate commit SHA")
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--context-output")
    args = parser.parse_args(argv)
    try:
        if _FULL_OID.fullmatch(args.source_ref) is None:
            raise ManifestBuildError("candidate must be a full commit SHA")
        repository = Path(args.repo).resolve(strict=True)
        if _git_oid(repository, f"{args.source_ref}^{{commit}}") != args.source_ref:
            raise ManifestBuildError("candidate must identify the commit itself")
        manifest = build_release_manifest(
            repository=args.repo,
            contract_path=args.contract,
            metadata_path=args.metadata,
            source_ref=args.source_ref,
            release_id=args.release_id,
            output=args.output,
        )
        if args.context_output is not None:
            context_commit, context_tree = materialize_build_context(
                repository=args.repo,
                source_ref=manifest.source_commit,
                output=args.context_output,
            )
            if (
                context_commit != manifest.source_commit
                or context_tree != manifest.source_tree
            ):
                raise ManifestBuildError(
                    "materialized build context differs from the release manifest"
                )
    except (ContractError, ManifestBuildError, ReleaseManifestError, OSError) as exc:
        print(f"release-manifest=failed ({type(exc).__name__})", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "generated",
                "manifest_hash": manifest.manifest_hash,
                "source_commit": manifest.source_commit,
                "source_tree": manifest.source_tree,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
