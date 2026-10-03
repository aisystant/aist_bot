from __future__ import annotations

import gzip
import hashlib
import io
import json
import socket
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from release_manifest import canonical_json_bytes
from scripts import check_release_image as verifier


def _digest(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _file(name, payload):
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    return member, payload


def _tar(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for member, payload in entries:
            archive.addfile(member, io.BytesIO(payload))
    return buffer.getvalue()


class ImageFixture:
    def __init__(self, directory):
        self.archive_path = directory / "image.tar"
        self.expected_path = directory / "expected.json"
        self.expected = (
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "release_id": "image-test",
                    "source_commit": "1" * 40,
                    "source_tree": "2" * 40,
                    "build_contract_digest": "sha256:" + "a" * 64,
                    "migration_class": "none",
                    "schema_min": 0,
                    "schema_max": 0,
                    "required_checks": ["release-candidate-build"],
                    "platform": {"os": "linux", "architecture": "amd64"},
                }
            )
            + b"\n"
        )
        self.expected_path.write_bytes(self.expected)
        self.blobs = {}
        self.layers = []
        self.config = {
            "os": "linux",
            "architecture": "amd64",
            "rootfs": {"type": "layers", "diff_ids": []},
        }
        self.platform = {"os": "linux", "architecture": "amd64"}
        self.add_layer(_tar([_file("app/release-manifest.json", self.expected)]))

    def blob(self, payload, media_type, **metadata):
        digest = _digest(payload)
        self.blobs["blobs/sha256/" + digest[7:]] = payload
        return {
            "mediaType": media_type,
            "digest": digest,
            "size": len(payload),
            **metadata,
        }

    def json_blob(self, payload, media_type, **metadata):
        return self.blob(canonical_json_bytes(payload), media_type, **metadata)

    def add_layer(self, payload, media_type=verifier.OCI_GZIP):
        stored = (
            gzip.compress(payload, mtime=0)
            if media_type == verifier.OCI_GZIP
            else payload
        )
        self.layers.append(self.blob(stored, media_type))
        self.config["rootfs"]["diff_ids"].append(_digest(payload))

    def image(self):
        config = self.json_blob(self.config, verifier.OCI_CONFIG)
        return self.json_blob(
            {
                "schemaVersion": 2,
                "mediaType": verifier.OCI_MANIFEST,
                "config": config,
                "layers": self.layers,
            },
            verifier.OCI_MANIFEST,
            platform=self.platform,
        )

    def index(self, descriptors):
        return self.json_blob(
            {
                "schemaVersion": 2,
                "mediaType": verifier.OCI_INDEX,
                "manifests": descriptors,
            },
            verifier.OCI_INDEX,
        )

    def attestation(self, image):
        layer = self.json_blob({"subject": [], "predicate": {}}, verifier.IN_TOTO)
        config = self.json_blob(
            {
                "os": "unknown",
                "architecture": "unknown",
                "rootfs": {"type": "layers", "diff_ids": [layer["digest"]]},
            },
            verifier.OCI_CONFIG,
        )
        return self.json_blob(
            {
                "schemaVersion": 2,
                "mediaType": verifier.OCI_MANIFEST,
                "config": config,
                "layers": [layer],
            },
            verifier.OCI_MANIFEST,
            platform={"os": "unknown", "architecture": "unknown"},
            annotations={
                "vnd.docker.reference.type": "attestation-manifest",
                "vnd.docker.reference.digest": image["digest"],
            },
        )

    def write(self, roots=None, extras=()):
        roots = roots if roots is not None else [self.image()]
        index = canonical_json_bytes({"schemaVersion": 2, "manifests": roots})
        self.archive_path.write_bytes(
            _tar(
                [
                    _file("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
                    _file("index.json", index),
                    *(_file(name, payload) for name, payload in self.blobs.items()),
                    *extras,
                ]
            )
        )
        return index

    def verify(self):
        return verifier.verify_release_image(self.archive_path, self.expected_path)

    def arguments(self):
        return [
            "--archive",
            str(self.archive_path),
            "--embedded-manifest",
            str(self.expected_path),
        ]


@pytest.fixture
def image_fixture(tmp_path):
    return ImageFixture(tmp_path)


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_index_manifest_and_unsigned_attestations_remain_distinct(image_fixture, depth):
    fixture = image_fixture
    image = fixture.image()
    roots = [image, fixture.attestation(image)]
    for _ in range(depth):
        roots = [fixture.index(roots)]
    raw_index = fixture.write(roots)
    result = fixture.verify()
    assert result["manifest_digest"] == image["digest"]
    assert result["layout_index_digest"] == _digest(raw_index)
    assert result["index_digest"] == (
        roots[0]["digest"] if depth else _digest(raw_index)
    )
    assert result["index_digest"] != result["manifest_digest"]
    assert result["source_commit"] == "1" * 40
    assert result["embedded_manifest_hash"] == _digest(fixture.expected.rstrip(b"\n"))
    assert result["attestation_count"] == 1
    assert result["preparation_only"] is True
    assert result["mutation_authorized"] is False
    assert result["signature_verified"] is False


def test_uncompressed_layers_and_base_symlinks_are_supported(image_fixture):
    fixture = image_fixture
    link = tarfile.TarInfo("bin/python")
    link.type, link.linkname = tarfile.SYMTYPE, "/usr/bin/python"
    fixture.layers.clear()
    fixture.config["rootfs"]["diff_ids"].clear()
    fixture.add_layer(_tar([(link, b"")]), verifier.OCI_LAYER)
    fixture.add_layer(
        _tar([_file("app/release-manifest.json", fixture.expected)]), verifier.OCI_LAYER
    )
    fixture.write()
    assert fixture.verify()["layer_count"] == 2


@pytest.mark.parametrize("blob_kind", ["image", "config", "layer", "attestation"])
def test_raw_blob_tampering_fails_even_when_size_is_preserved(image_fixture, blob_kind):
    fixture = image_fixture
    image = fixture.image()
    attestation = fixture.attestation(image)
    image_payload = json.loads(fixture.blobs["blobs/sha256/" + image["digest"][7:]])
    descriptor = {
        "image": image,
        "config": image_payload["config"],
        "layer": fixture.layers[0],
        "attestation": attestation,
    }[blob_kind]
    name = "blobs/sha256/" + descriptor["digest"][7:]
    payload = fixture.blobs[name]
    fixture.blobs[name] = bytes([payload[0] ^ 1]) + payload[1:]
    fixture.write([image, attestation])
    with pytest.raises(verifier.ImageVerificationError, match="raw digest"):
        fixture.verify()


@pytest.mark.parametrize("field", ["size", "digest"])
def test_descriptor_must_address_exact_local_blob(image_fixture, field):
    fixture = image_fixture
    image = fixture.image()
    image[field] = image["size"] + 1 if field == "size" else "sha256:" + "0" * 64
    fixture.write([image])
    with pytest.raises(verifier.ImageVerificationError, match="missing or wrong-size"):
        fixture.verify()


@pytest.mark.parametrize("field", ["platform", "config"])
def test_platform_must_match_config_and_linux_amd64(image_fixture, field):
    fixture = image_fixture
    if field == "platform":
        fixture.platform["architecture"] = "arm64"
    else:
        fixture.config["architecture"] = "arm64"
        fixture.platform["architecture"] = "arm64"
    fixture.write()
    with pytest.raises(verifier.ImageVerificationError, match="platform"):
        fixture.verify()


def test_multiple_runnable_images_are_not_silently_selected(image_fixture):
    fixture = image_fixture
    first = fixture.image()
    fixture.config["created"] = "different image"
    second = fixture.image()
    fixture.write([first, second])
    with pytest.raises(verifier.ImageVerificationError, match="exactly one runnable"):
        fixture.verify()


def test_duplicate_manifest_reference_is_rejected(image_fixture):
    fixture = image_fixture
    image = fixture.image()
    fixture.write([image, image])
    with pytest.raises(verifier.ImageVerificationError, match="duplicate"):
        fixture.verify()


def test_attestation_cannot_hide_a_second_runnable_image(image_fixture):
    fixture = image_fixture
    image = fixture.image()
    image["annotations"] = {"vnd.docker.reference.type": "attestation-manifest"}
    fixture.write([image])
    with pytest.raises(verifier.ImageVerificationError, match="masquerade"):
        fixture.verify()


def test_attestation_must_reference_selected_image(image_fixture):
    fixture = image_fixture
    image = fixture.image()
    attestation = fixture.attestation(image)
    attestation["annotations"]["vnd.docker.reference.digest"] = "sha256:" + "0" * 64
    fixture.write([image, attestation])
    with pytest.raises(verifier.ImageVerificationError, match="not linked"):
        fixture.verify()


@pytest.mark.parametrize("diff_ids", [[], ["sha256:" + "0" * 64]])
def test_rootfs_diff_ids_are_verified(image_fixture, diff_ids):
    fixture = image_fixture
    fixture.config["rootfs"]["diff_ids"] = diff_ids
    fixture.write()
    with pytest.raises(verifier.ImageVerificationError, match="diff ID"):
        fixture.verify()


@pytest.mark.parametrize("name", ["different.json", "app/release-manifest.json"])
def test_manifest_must_be_exact_and_in_the_last_layer(image_fixture, name):
    fixture = image_fixture
    fixture.add_layer(_tar([_file(name, b"wrong bytes")]))
    fixture.write()
    with pytest.raises(verifier.ImageVerificationError, match="manifest"):
        fixture.verify()


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/app/file",
        "app/../file",
        "app\\file",
        "app/.wh.release-manifest.json",
    ],
)
def test_final_layer_rejects_unsafe_paths_and_whiteouts(image_fixture, name):
    fixture = image_fixture
    fixture.add_layer(
        _tar([_file("app/release-manifest.json", fixture.expected), _file(name, b"x")])
    )
    fixture.write()
    with pytest.raises(verifier.ImageVerificationError, match="unsafe path|whiteout"):
        fixture.verify()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_final_layer_rejects_links_and_special_files(image_fixture, kind):
    fixture = image_fixture
    member = tarfile.TarInfo("app/release-manifest.json")
    member.type, member.linkname = kind, "elsewhere"
    fixture.add_layer(_tar([(member, b"")]))
    fixture.write()
    with pytest.raises(verifier.ImageVerificationError, match="links, and special"):
        fixture.verify()


@pytest.mark.parametrize("location", ["outer", "final"])
def test_duplicate_tar_paths_include_normalized_aliases(image_fixture, location):
    fixture = image_fixture
    if location == "outer":
        fixture.write(extras=[_file("./index.json", b"{}")])
    else:
        fixture.add_layer(
            _tar(
                [
                    _file("app/release-manifest.json", fixture.expected),
                    _file("./app/release-manifest.json", fixture.expected),
                ]
            )
        )
        fixture.write()
    with pytest.raises(verifier.ImageVerificationError, match="duplicate"):
        fixture.verify()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "blobs\\escape"])
def test_outer_archive_rejects_traversal(image_fixture, name):
    image_fixture.write(extras=[_file(name, b"x")])
    with pytest.raises(verifier.ImageVerificationError, match="unsafe path"):
        image_fixture.verify()


def test_outer_archive_rejects_symlink(image_fixture):
    member = tarfile.TarInfo("linked")
    member.type, member.linkname = tarfile.SYMTYPE, "index.json"
    image_fixture.write(extras=[(member, b"")])
    with pytest.raises(verifier.ImageVerificationError, match="symlinks"):
        image_fixture.verify()


def test_unsupported_zstd_fails_explicitly(image_fixture):
    image_fixture.layers[0]["mediaType"] = verifier.OCI_LAYER + "+zstd"
    image_fixture.write()
    with pytest.raises(verifier.ImageVerificationError, match="zstd"):
        image_fixture.verify()


@pytest.mark.parametrize(
    "limit",
    [
        "MAX_FINAL_LAYER_BYTES",
        "MAX_TOTAL_UNPACKED_BYTES",
        "MAX_ARCHIVE_BYTES",
        "MAX_MEMBERS",
    ],
)
def test_resource_limits_fail_closed(image_fixture, monkeypatch, limit):
    image_fixture.write()
    monkeypatch.setattr(verifier, limit, 1)
    with pytest.raises(verifier.ImageVerificationError, match="limit|bounded"):
        image_fixture.verify()


def test_index_nesting_is_bounded(image_fixture):
    fixture = image_fixture
    roots = [fixture.image()]
    for _ in range(verifier.MAX_INDEX_DEPTH + 1):
        roots = [fixture.index(roots)]
    fixture.write(roots)
    with pytest.raises(verifier.ImageVerificationError, match="nesting"):
        fixture.verify()


def test_pax_header_allocation_is_bounded(image_fixture, monkeypatch):
    member = tarfile.TarInfo("pax")
    member.type, member.size = tarfile.XHDTYPE, 2048
    image_fixture.archive_path.write_bytes(
        member.tobuf(format=tarfile.USTAR_FORMAT) + b"0" * 2048 + b"\0" * 1024
    )
    monkeypatch.setattr(verifier, "MAX_METADATA_BYTES", 1024)
    with pytest.raises(verifier.ImageVerificationError, match="tar metadata"):
        image_fixture.verify()


def test_cli_has_no_extraction_execution_network_or_file_writes(
    image_fixture, monkeypatch, capsys
):
    fixture = image_fixture
    fixture.write()
    original = {
        path: path.read_bytes() for path in fixture.archive_path.parent.iterdir()
    }

    def forbidden(*args, **kwargs):
        pytest.fail("verification must not execute, extract, or use the network")

    monkeypatch.setattr(tarfile.TarFile, "extract", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extractall", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert verifier.main(fixture.arguments()) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == ""
    assert output.out.encode() == canonical_json_bytes(result) + b"\n"
    assert result["preparation_only"] is True
    assert {
        path: path.read_bytes() for path in fixture.archive_path.parent.iterdir()
    } == original


@pytest.mark.parametrize(
    "bad_input", [b"{", b"\xff", b"NaN", b'{"schema_version":1,"schema_version":1}']
)
def test_invalid_expected_manifest_has_no_partial_stdout(
    image_fixture, bad_input, capsys
):
    image_fixture.write()
    image_fixture.expected_path.write_bytes(bad_input)
    assert verifier.main(image_fixture.arguments()) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "release-image=failed" in output.err


def test_noncanonical_expected_manifest_is_rejected(image_fixture):
    image_fixture.write()
    image_fixture.expected_path.write_text(
        json.dumps(json.loads(image_fixture.expected), indent=2)
    )
    with pytest.raises(verifier.ImageVerificationError, match="canonical JSON"):
        image_fixture.verify()


def test_cli_rejects_archive_symlink_without_partial_stdout(image_fixture, capsys):
    fixture = image_fixture
    fixture.write()
    link = fixture.archive_path.parent / "linked.tar"
    link.symlink_to(fixture.archive_path)
    args = fixture.arguments()
    args[1] = str(link)
    assert verifier.main(args) == 2
    assert capsys.readouterr().out == ""


def test_cli_imports_schema_in_isolated_python(image_fixture):
    image_fixture.write()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(Path(verifier.__file__)),
            *image_fixture.arguments(),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mutation_authorized"] is False
    assert result.stderr == ""


@pytest.mark.parametrize("missing", ["index.json", "oci-layout", "layer"])
def test_missing_archive_members_fail_without_partial_stdout(
    image_fixture, missing, capsys
):
    fixture = image_fixture
    fixture.write()
    missing_name = (
        "blobs/sha256/" + fixture.layers[0]["digest"][7:]
        if missing == "layer"
        else missing
    )
    with tarfile.open(fixture.archive_path, "r:") as archive:
        entries = [
            _file(member.name, archive.extractfile(member).read())
            for member in archive
            if member.name != missing_name
        ]
    fixture.archive_path.write_bytes(_tar(entries))
    assert verifier.main(fixture.arguments()) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "release-image=failed" in output.err


def test_base_layer_diff_id_is_verified_even_when_final_manifest_matches(image_fixture):
    fixture = image_fixture
    fixture.config["rootfs"]["diff_ids"][0] = "sha256:" + "0" * 64
    fixture.add_layer(_tar([_file("app/release-manifest.json", fixture.expected)]))
    fixture.write()
    with pytest.raises(verifier.ImageVerificationError, match="diff ID mismatch"):
        fixture.verify()


def test_forged_descriptor_boolean_size_is_rejected(image_fixture):
    fixture = image_fixture
    image = fixture.image()
    image["size"] = True
    fixture.write([image])
    with pytest.raises(
        verifier.ImageVerificationError, match="descriptor digest, size"
    ):
        fixture.verify()


def test_corrupt_gzip_fails_without_partial_stdout(image_fixture, capsys):
    fixture = image_fixture
    fixture.layers[0] = fixture.blob(b"invalid gzip bytes", verifier.OCI_GZIP)
    fixture.write()
    assert verifier.main(fixture.arguments()) == 2
    assert capsys.readouterr().out == ""
