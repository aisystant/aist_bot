from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from release_manifest import canonical_sha256
from scripts import build_release_manifest as build_release_manifest_module
from scripts.build_release_manifest import (
    ManifestBuildError,
    build_release_manifest,
    main,
    materialize_build_context,
)

REPO_ROOT = Path(__file__).parents[1]


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "WP562 Test")
    _git(repository, "config", "user.email", "wp562@example.invalid")
    (repository / "payload.txt").write_text("candidate\n", encoding="utf-8")
    (repository / ".github").mkdir()
    shutil.copyfile(
        REPO_ROOT / ".github" / "release-control-contract.json",
        repository / ".github" / "release-control-contract.json",
    )
    # Explicit disposable-fixture declaration, never production schema evidence.
    (repository / ".github" / "release-metadata.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "declaration_revision": 1,
                "migration_class": "expand",
                "schema_min": 4,
                "schema_max": 6,
            }
        ),
        encoding="utf-8",
    )
    _git(
        repository,
        "add",
        "payload.txt",
        ".github/release-control-contract.json",
        ".github/release-metadata.json",
    )
    _git(repository, "commit", "-qm", "fixture")
    return repository


def test_builder_binds_exact_commit_tree_contract_and_checks(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    manifest = build_release_manifest(
        repository=repository,
        contract_path=".github/release-control-contract.json",
        metadata_path=".github/release-metadata.json",
        source_ref="HEAD",
        release_id="release-test-1",
        output="release-manifest.candidate.json",
    )

    payload = json.loads(
        (repository / "release-manifest.candidate.json").read_text(encoding="utf-8")
    )
    contract = json.loads(
        (repository / ".github" / "release-control-contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload == manifest.to_mapping()
    assert payload["source_commit"] == _git(repository, "rev-parse", "HEAD")
    assert payload["source_tree"] == _git(repository, "rev-parse", "HEAD^{tree}")
    assert payload["build_contract_digest"] == canonical_sha256(contract)
    assert payload["required_checks"] == contract["required_checks"]
    assert payload["migration_class"] == "expand"
    assert payload["schema_min"] == 4
    assert payload["schema_max"] == 6


def test_builder_is_deterministic_for_the_same_inputs(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    arguments = {
        "repository": repository,
        "contract_path": ".github/release-control-contract.json",
        "metadata_path": ".github/release-metadata.json",
        "source_ref": "HEAD",
        "release_id": "release-test-1",
        "output": "release-manifest.candidate.json",
    }

    first = build_release_manifest(**arguments)
    first_bytes = (repository / arguments["output"]).read_bytes()
    second = build_release_manifest(**arguments)

    assert first.manifest_hash == second.manifest_hash
    assert (repository / arguments["output"]).read_bytes() == first_bytes


def test_builder_rejects_output_outside_repository(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ManifestBuildError):
        build_release_manifest(
            repository=repository,
            contract_path=".github/release-control-contract.json",
            metadata_path=".github/release-metadata.json",
            source_ref="HEAD",
            release_id="release-test-1",
            output=tmp_path / "release-manifest.escape.json",
        )


def test_builder_uses_committed_contract_when_worktree_copy_changes(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    contract_path = repository / ".github" / "release-control-contract.json"
    committed_contract = json.loads(
        _git(repository, "show", "HEAD:.github/release-control-contract.json")
    )
    contract_path.write_text("{}\n", encoding="utf-8")

    manifest = build_release_manifest(
        repository=repository,
        contract_path=contract_path,
        metadata_path=".github/release-metadata.json",
        source_ref="HEAD",
        release_id="release-test-1",
        output="release-manifest.candidate.json",
    )

    assert manifest.build_contract_digest == canonical_sha256(committed_contract)


def test_builder_uses_committed_metadata_when_worktree_copy_changes(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    metadata_path = repository / ".github" / "release-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["migration_class"] = "data_rewrite"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    manifest = build_release_manifest(
        repository=repository,
        contract_path=".github/release-control-contract.json",
        metadata_path=metadata_path,
        source_ref="HEAD",
        release_id="release-test-1",
        output="release-manifest.candidate.json",
    )

    assert manifest.migration_class.value == "expand"
    assert (manifest.schema_min, manifest.schema_max) == (4, 6)


@pytest.mark.parametrize(
    ("migration_class", "schema_min", "schema_max"),
    [("none", 9, 7), ("expand", 0, 0)],
)
def test_builder_rejects_semantically_impossible_committed_metadata(
    tmp_path: Path,
    migration_class: str,
    schema_min: int,
    schema_max: int,
) -> None:
    repository = _repository(tmp_path)
    metadata_path = repository / ".github" / "release-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(
        migration_class=migration_class,
        schema_min=schema_min,
        schema_max=schema_max,
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _git(repository, "add", ".github/release-metadata.json")
    _git(repository, "commit", "-qm", "invalid migration semantics")

    with pytest.raises(ManifestBuildError, match="migration semantics"):
        build_release_manifest(
            repository=repository,
            contract_path=".github/release-control-contract.json",
            metadata_path=".github/release-metadata.json",
            source_ref="HEAD",
            release_id="release-test-1",
            output="release-manifest.candidate.json",
        )


def test_builder_preserves_no_migration_compatibility_from_committed_metadata(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    metadata_path = repository / ".github" / "release-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(migration_class="none", schema_min=7, schema_max=9)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _git(repository, "add", ".github/release-metadata.json")
    _git(repository, "commit", "-qm", "explicit fixture compatibility declaration")
    candidate = _git(repository, "rev-parse", "HEAD")

    manifest = build_release_manifest(
        repository=repository,
        contract_path=".github/release-control-contract.json",
        metadata_path=".github/release-metadata.json",
        source_ref=candidate,
        release_id="release-test-1",
        output="release-manifest.candidate.json",
    )

    payload = json.loads((repository / "release-manifest.candidate.json").read_bytes())
    assert payload == manifest.to_mapping()
    assert payload["source_commit"] == candidate
    assert payload["migration_class"] == "none"
    assert (payload["schema_min"], payload["schema_max"]) == (7, 9)


def test_context_materializer_ignores_archive_export_transformations(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / ".gitattributes").write_text(
        "payload.txt export-ignore\nsubstituted.txt export-subst\n",
        encoding="utf-8",
    )
    substituted = "$Format:%H$\n"
    (repository / "substituted.txt").write_text(substituted, encoding="utf-8")
    _git(repository, "add", ".gitattributes", "substituted.txt")
    _git(repository, "commit", "-qm", "archive transformation fixture")

    commit, tree = materialize_build_context(
        repository=repository,
        source_ref="HEAD",
    )

    context = repository / "wp562-build-context"
    assert commit == _git(repository, "rev-parse", "HEAD")
    assert tree == _git(repository, "rev-parse", "HEAD^{tree}")
    assert (context / "payload.txt").read_text(encoding="utf-8") == "candidate\n"
    assert (context / "substituted.txt").read_text(encoding="utf-8") == substituted


def test_context_materializer_rejects_a_symlink(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    os.symlink("payload.txt", repository / "payload-link")
    _git(repository, "add", "payload-link")
    _git(repository, "commit", "-qm", "symlink fixture")

    with pytest.raises(ManifestBuildError, match="symlink, gitlink"):
        materialize_build_context(repository=repository, source_ref="HEAD")


def test_context_materializer_rejects_a_gitlink(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit},vendor/linked",
    )
    _git(repository, "commit", "-qm", "gitlink fixture")

    with pytest.raises(ManifestBuildError, match="symlink, gitlink"):
        materialize_build_context(repository=repository, source_ref="HEAD")


def test_cli_materializes_the_manifest_commit_when_source_ref_moves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    original_builder = build_release_manifest_module.build_release_manifest
    candidate_commit = _git(repository, "rev-parse", "HEAD")

    def build_then_move_ref(**kwargs):
        manifest = original_builder(**kwargs)
        (repository / "payload.txt").write_text("moved\n", encoding="utf-8")
        _git(repository, "add", "payload.txt")
        _git(repository, "commit", "-qm", "move mutable source ref")
        return manifest

    monkeypatch.setattr(
        build_release_manifest_module,
        "build_release_manifest",
        build_then_move_ref,
    )

    result = main(
        [
            "--repo",
            str(repository),
            "--contract",
            ".github/release-control-contract.json",
            "--metadata",
            ".github/release-metadata.json",
            "--source-ref",
            candidate_commit,
            "--release-id",
            "release-ref-race",
            "--output",
            "release-manifest.candidate.json",
            "--context-output",
            "wp562-build-context",
        ]
    )

    manifest = json.loads(
        (repository / "release-manifest.candidate.json").read_text(encoding="utf-8")
    )
    assert result == 0
    assert manifest["source_commit"] != _git(repository, "rev-parse", "HEAD")
    assert (repository / "wp562-build-context" / "payload.txt").read_text(
        encoding="utf-8"
    ) == "candidate\n"


def _cli_arguments(repository: Path, source_ref: str) -> list[str]:
    return [
        "--repo",
        str(repository),
        "--contract",
        ".github/release-control-contract.json",
        "--metadata",
        ".github/release-metadata.json",
        "--source-ref",
        source_ref,
        "--release-id",
        "release-test-1",
        "--output",
        "release-manifest.candidate.json",
        "--context-output",
        "wp562-build-context",
    ]


@pytest.mark.parametrize("metadata_state", ["missing", "untracked"])
def test_cli_refuses_missing_committed_metadata_without_creating_outputs(
    tmp_path: Path, metadata_state: str, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    metadata = repository / ".github/release-metadata.json"
    fixture_bytes = metadata.read_bytes()
    _git(repository, "rm", ".github/release-metadata.json")
    _git(repository, "commit", "-qm", "candidate has no reviewed metadata")
    if metadata_state == "untracked":
        metadata.write_bytes(fixture_bytes)

    result = main(_cli_arguments(repository, _git(repository, "rev-parse", "HEAD")))

    assert result == 2
    assert capsys.readouterr().out == ""
    assert not (repository / "release-manifest.candidate.json").exists()
    assert not (repository / "wp562-build-context").exists()
    assert metadata.exists() is (metadata_state == "untracked")
    if metadata_state == "untracked":
        assert metadata.read_bytes() == fixture_bytes


@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        "{}",
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":NaN}',
    ],
)
def test_cli_refuses_corrupt_committed_metadata_without_creating_outputs(
    tmp_path: Path, raw: str, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    metadata = repository / ".github/release-metadata.json"
    metadata.write_text(raw, encoding="utf-8")
    _git(repository, "add", ".github/release-metadata.json")
    _git(repository, "commit", "-qm", "invalid metadata fixture")

    result = main(_cli_arguments(repository, _git(repository, "rev-parse", "HEAD")))

    assert result == 2
    assert capsys.readouterr().out == ""
    assert metadata.read_text(encoding="utf-8") == raw
    assert not (repository / "release-manifest.candidate.json").exists()
    assert not (repository / "wp562-build-context").exists()


@pytest.mark.parametrize(
    "source_kind", ["head", "abbreviated", "annotated_tag", "tree", "missing"]
)
def test_cli_requires_a_full_existing_commit_object(
    tmp_path: Path, source_kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "-a", "candidate", "-m", "annotated tag fixture")
    refs = {
        "head": "HEAD",
        "abbreviated": commit[:12],
        "annotated_tag": _git(repository, "rev-parse", "candidate"),
        "tree": _git(repository, "rev-parse", "HEAD^{tree}"),
        "missing": "f" * 40,
    }

    result = main(_cli_arguments(repository, refs[source_kind]))

    assert result == 2
    assert capsys.readouterr().out == ""
    assert not (repository / "release-manifest.candidate.json").exists()
    assert not (repository / "wp562-build-context").exists()


def test_cli_uses_original_commit_and_blobs_despite_git_replace_refs(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    original_commit = _git(repository, "rev-parse", "HEAD")
    original_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    original_blob = _git(repository, "rev-parse", "HEAD:payload.txt")
    (repository / "payload.txt").write_text("substituted\n", encoding="utf-8")
    _git(repository, "add", "payload.txt")
    _git(repository, "commit", "-qm", "substitute fixture")
    replacement_commit = _git(repository, "rev-parse", "HEAD")
    replacement_blob = _git(repository, "rev-parse", "HEAD:payload.txt")
    _git(repository, "replace", original_commit, replacement_commit)
    _git(repository, "replace", original_blob, replacement_blob)
    assert _git(repository, "show", f"{original_commit}:payload.txt") == "substituted"

    result = main(_cli_arguments(repository, original_commit))

    assert result == 0
    manifest = json.loads((repository / "release-manifest.candidate.json").read_bytes())
    assert manifest["source_commit"] == original_commit
    assert manifest["source_tree"] == original_tree
    assert (repository / "wp562-build-context/payload.txt").read_text(
        encoding="utf-8"
    ) == "candidate\n"


def test_context_contains_only_committed_bytes_despite_dirty_and_untracked_files(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    (repository / "payload.txt").write_text("local change\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("local only\n", encoding="utf-8")

    materialize_build_context(repository=repository, source_ref=commit)

    context = repository / "wp562-build-context"
    assert (context / "payload.txt").read_text(encoding="utf-8") == "candidate\n"
    assert not (context / "untracked.txt").exists()
    assert (repository / "payload.txt").read_text(encoding="utf-8") == "local change\n"


@pytest.mark.parametrize(
    "name", ["release-manifest.unavailable.json", "release-manifest.candidate.json"]
)
def test_builder_never_replaces_a_tracked_output(tmp_path: Path, name: str) -> None:
    repository = _repository(tmp_path)
    output = repository / name
    original = b'{"status":"unavailable"}\n'
    output.write_bytes(original)
    _git(repository, "add", name)
    _git(repository, "commit", "-qm", "tracked output fixture")

    with pytest.raises(ManifestBuildError, match="tracked file"):
        build_release_manifest(
            repository=repository,
            contract_path=".github/release-control-contract.json",
            metadata_path=".github/release-metadata.json",
            source_ref="HEAD",
            release_id="release-test-1",
            output=name,
        )

    assert output.read_bytes() == original
    assert _git(repository, "diff", "--", name) == ""
