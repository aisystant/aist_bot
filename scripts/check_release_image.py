"""Inspect a local OCI export without extracting, executing, or authorizing it.

Digest equality checks local bytes, not provenance signatures or deployment
readiness. Base filesystem layers may legitimately contain symlinks; only their
compressed digests and uncompressed diff IDs are inspected. The final COPY
layer and the outer archive admit only regular files and directories.
The complete layered filesystem is not reconstructed: matching last-layer
manifest bytes are preparation evidence, never a proof of runtime filesystem
contents or effective deployment identity.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import os
import re
import stat
import sys
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar"
OCI_GZIP = OCI_LAYER + "+gzip"
IN_TOTO = "application/vnd.in-toto+json"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
BLOB_PATH = re.compile(r"blobs/sha256/[0-9a-f]{64}\Z")
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_BLOB_BYTES = 1024**3
MAX_METADATA_BYTES = 4 * 1024**2
MAX_MANIFEST_BYTES = 64 * 1024
MAX_FINAL_LAYER_BYTES = 16 * 1024**2
MAX_UNPACKED_LAYER_BYTES = 2 * 1024**3
MAX_TOTAL_UNPACKED_BYTES = 8 * 1024**3
MAX_MEMBERS = 8192
MAX_DESCRIPTORS = 128
MAX_INDEX_DEPTH = 4
CHUNK_BYTES = 64 * 1024


class ImageVerificationError(ValueError):
    """The local archive does not meet the supported preparation contract."""


def _load_manifest_schema():
    path = Path(__file__).resolve().parents[1] / "release_manifest.py"
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ImageVerificationError("release manifest schema must be a regular file")
    name = "_wp562_image_manifest"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImageVerificationError("release manifest schema is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_schema = _load_manifest_schema()


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_object(payload: bytes, label: str) -> dict:
    try:
        value = _schema.loads_strict_json(payload)
    except (ValueError, RecursionError) as exc:
        raise ImageVerificationError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ImageVerificationError(f"{label} must be a JSON object")
    return value


@contextmanager
def _regular_input(path: Path, maximum: int):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            raise ImageVerificationError("input is not a bounded regular file")
        yield stream


class _TarReadGuard:
    """Bound extension-header allocations inside the standard tar parser."""

    def __init__(self, stream):
        self.stream = stream

    def read(self, size=-1):
        if not 0 <= size <= MAX_METADATA_BYTES:
            raise ImageVerificationError("tar metadata exceeds its size limit")
        return self.stream.read(size)

    def seek(self, offset, whence=0):
        return self.stream.seek(offset, whence)

    def tell(self):
        return self.stream.tell()


def _member_path(member: tarfile.TarInfo) -> str:
    name = member.name.removeprefix("./")
    name = name.rstrip("/") if member.isdir() else name
    if (
        not name
        or len(name) > 1024
        or "\\" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise ImageVerificationError("tar contains an unsafe path")
    if not (member.isfile() or member.isdir()) or member.issparse():
        raise ImageVerificationError(
            "tar symlinks, links, and special files are forbidden"
        )
    if member.isdir() and member.size:
        raise ImageVerificationError("tar directory contains unexpected data")
    return name


def _members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members = {}
    for member in archive:
        name = _member_path(member)
        if name in members:
            raise ImageVerificationError("tar contains duplicate paths")
        if len(members) >= MAX_MEMBERS or not 0 <= member.size <= MAX_BLOB_BYTES:
            raise ImageVerificationError("tar member count or size exceeds its limit")
        members[name] = member
    for name in members:
        parts = name.split("/")
        for length in range(1, len(parts)):
            ancestor = members.get("/".join(parts[:length]))
            if ancestor is not None and not ancestor.isdir():
                raise ImageVerificationError("tar file is used as a parent directory")
    return members


def _read_member(archive, member, maximum):
    if not member.isfile() or not 0 <= member.size <= maximum:
        raise ImageVerificationError("OCI metadata exceeds its size limit")
    with archive.extractfile(member) as stream:
        payload = stream.read(member.size + 1)
    if len(payload) != member.size:
        raise ImageVerificationError("OCI archive member is truncated")
    return payload


@dataclass(frozen=True)
class _Descriptor:
    media_type: str
    digest: str
    size: int
    platform: dict | None
    annotations: dict

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict):
            raise ImageVerificationError("OCI descriptor must be an object")
        digest, size = value.get("digest"), value.get("size")
        if (
            not isinstance(digest, str)
            or DIGEST.fullmatch(digest) is None
            or type(size) is not int
            or not 0 < size <= MAX_BLOB_BYTES
            or not isinstance(value.get("mediaType"), str)
            or "data" in value
            or "urls" in value
        ):
            raise ImageVerificationError(
                "OCI descriptor digest, size, or locality is invalid"
            )
        platform = value.get("platform")
        annotations = value.get("annotations", {})
        if (
            (platform is not None and not isinstance(platform, dict))
            or not isinstance(annotations, dict)
            or any(
                not isinstance(key, str) or not isinstance(item, str)
                for key, item in annotations.items()
            )
        ):
            raise ImageVerificationError("OCI descriptor metadata is invalid")
        return cls(value["mediaType"], digest, size, platform, annotations)


def _descriptor_list(value, label):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_DESCRIPTORS:
        raise ImageVerificationError(
            f"{label} requires a bounded nonempty descriptor list"
        )
    return [_Descriptor.parse(item) for item in value]


def _document_type(value, media_type):
    if (
        type(value.get("schemaVersion")) is not int
        or value["schemaVersion"] != 2
        or value.get("mediaType", media_type) != media_type
    ):
        raise ImageVerificationError("OCI document schema or media type is invalid")


class _OCIArchive:
    def __init__(self, archive):
        self.archive = archive
        self.members = _members(archive)
        for name, member in self.members.items():
            if member.isdir():
                if name not in {"blobs", "blobs/sha256"}:
                    raise ImageVerificationError(
                        "OCI archive contains an unexpected directory"
                    )
                continue
            if name in {"index.json", "oci-layout"}:
                continue
            if BLOB_PATH.fullmatch(name) is None:
                raise ImageVerificationError("OCI archive contains an unexpected file")
            digest = hashlib.sha256()
            with archive.extractfile(member) as stream:
                while chunk := stream.read(CHUNK_BYTES):
                    digest.update(chunk)
            if digest.hexdigest() != name.rsplit("/", 1)[-1]:
                raise ImageVerificationError("OCI blob raw digest mismatch")

    def named_bytes(self, name, maximum):
        member = self.members.get(name)
        if member is None:
            raise ImageVerificationError("OCI archive is missing a required member")
        return _read_member(self.archive, member, maximum)

    def blob(self, descriptor):
        name = "blobs/sha256/" + descriptor.digest.removeprefix("sha256:")
        member = self.members.get(name)
        if member is None or not member.isfile() or member.size != descriptor.size:
            raise ImageVerificationError(
                "OCI descriptor references a missing or wrong-size blob"
            )
        return member

    def json_blob(self, descriptor):
        return _json_object(
            _read_member(self.archive, self.blob(descriptor), MAX_METADATA_BYTES),
            "OCI blob",
        )

    def manifests(self, index):
        found = []
        seen = set()

        def walk(document, depth):
            if depth > MAX_INDEX_DEPTH:
                raise ImageVerificationError("OCI index nesting exceeds its limit")
            _document_type(document, OCI_INDEX)
            for descriptor in _descriptor_list(document.get("manifests"), "OCI index"):
                if descriptor.digest in seen or len(seen) >= MAX_DESCRIPTORS:
                    raise ImageVerificationError(
                        "OCI index has duplicate or excessive descriptors"
                    )
                seen.add(descriptor.digest)
                payload = self.json_blob(descriptor)
                if descriptor.media_type == OCI_INDEX:
                    walk(payload, depth + 1)
                elif descriptor.media_type == OCI_MANIFEST:
                    _document_type(payload, OCI_MANIFEST)
                    found.append((descriptor, payload))
                else:
                    raise ImageVerificationError(
                        "unsupported OCI index descriptor media type"
                    )

        walk(index, 0)
        return found


def _config_and_layers(archive, descriptor, manifest):
    config_descriptor = _Descriptor.parse(manifest.get("config"))
    if config_descriptor.media_type != OCI_CONFIG:
        raise ImageVerificationError("unsupported OCI config media type")
    config = archive.json_blob(config_descriptor)
    layers = _descriptor_list(manifest.get("layers"), "OCI manifest layers")
    for layer in layers:
        archive.blob(layer)
    rootfs = config.get("rootfs")
    if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
        raise ImageVerificationError("OCI config rootfs is invalid")
    diff_ids = rootfs.get("diff_ids")
    if (
        not isinstance(diff_ids, list)
        or len(diff_ids) != len(layers)
        or any(
            not isinstance(item, str) or DIGEST.fullmatch(item) is None
            for item in diff_ids
        )
    ):
        raise ImageVerificationError("OCI config diff IDs do not match layer count")
    platform = {field: config.get(field) for field in ("os", "architecture")}
    if descriptor.platform is not None and any(
        descriptor.platform.get(field) != expected
        for field, expected in platform.items()
    ):
        raise ImageVerificationError("OCI descriptor and config platform mismatch")
    return config_descriptor, platform, layers, diff_ids


def _runnable(archive, manifests):
    runnable = []
    attestations = []
    for descriptor, manifest in manifests:
        config, platform, layers, diff_ids = _config_and_layers(
            archive, descriptor, manifest
        )
        if (
            descriptor.annotations.get("vnd.docker.reference.type")
            == "attestation-manifest"
        ):
            if platform != {"os": "unknown", "architecture": "unknown"}:
                raise ImageVerificationError(
                    "attestation must not masquerade as a runnable image"
                )
            for layer, diff_id in zip(layers, diff_ids, strict=True):
                if layer.media_type != IN_TOTO or layer.digest != diff_id:
                    raise ImageVerificationError(
                        "unsupported attestation layer or diff ID"
                    )
                archive.json_blob(layer)
            attestations.append(descriptor)
        else:
            if platform != {"os": "linux", "architecture": "amd64"}:
                raise ImageVerificationError(
                    "runnable image platform must be linux/amd64"
                )
            runnable.append((descriptor, config, layers, diff_ids))
    if len(runnable) != 1:
        raise ImageVerificationError(
            "OCI index must resolve to exactly one runnable image"
        )
    image = runnable[0]
    if any(
        item.annotations.get("vnd.docker.reference.digest") != image[0].digest
        for item in attestations
    ):
        raise ImageVerificationError(
            "attestation is not linked to the runnable manifest"
        )
    return image, len(attestations)


def _layer_chunks(archive, descriptor, maximum):
    if descriptor.media_type not in {OCI_LAYER, OCI_GZIP}:
        raise ImageVerificationError(
            "unsupported layer compression/media type (including zstd)"
        )
    with (
        archive.archive.extractfile(archive.blob(descriptor)) as raw,
        gzip.GzipFile(fileobj=raw)
        if descriptor.media_type == OCI_GZIP
        else raw as stream,
    ):
        size = 0
        while chunk := stream.read(CHUNK_BYTES):
            size += len(chunk)
            if size > maximum:
                raise ImageVerificationError("unpacked layer exceeds its size limit")
            yield chunk


def _verified_final_layer(archive, layers, diff_ids):
    remaining = MAX_TOTAL_UNPACKED_BYTES
    final = b""
    for number, (layer, expected) in enumerate(zip(layers, diff_ids, strict=True)):
        maximum = (
            MAX_FINAL_LAYER_BYTES
            if number == len(layers) - 1
            else MAX_UNPACKED_LAYER_BYTES
        )
        digest = hashlib.sha256()
        captured = []
        for chunk in _layer_chunks(archive, layer, min(maximum, remaining)):
            remaining -= len(chunk)
            digest.update(chunk)
            if number == len(layers) - 1:
                captured.append(chunk)
        if "sha256:" + digest.hexdigest() != expected:
            raise ImageVerificationError(
                "OCI config uncompressed layer diff ID mismatch"
            )
        if captured:
            final = b"".join(captured)
    return final


def _check_packaged_manifest(layer, expected):
    with tarfile.open(fileobj=_TarReadGuard(io.BytesIO(layer)), mode="r:") as archive:
        members = _members(archive)
        if any(part.startswith(".wh.") for name in members for part in name.split("/")):
            raise ImageVerificationError("final manifest layer contains a whiteout")
        member = members.get("app/release-manifest.json")
        if member is None or not member.isfile():
            raise ImageVerificationError(
                "final layer is missing the regular embedded manifest"
            )
        if _read_member(archive, member, MAX_MANIFEST_BYTES) != expected:
            raise ImageVerificationError(
                "packaged manifest bytes do not match the expected manifest"
            )


def verify_release_image(archive_path: Path, embedded_manifest_path: Path) -> dict:
    with _regular_input(embedded_manifest_path, MAX_MANIFEST_BYTES) as stream:
        expected = stream.read(MAX_MANIFEST_BYTES + 1)
    manifest = _schema.EmbeddedManifest.from_mapping(
        _json_object(expected, "embedded manifest")
    )
    canonical = _schema.canonical_json_bytes(manifest.to_mapping())
    if expected not in (canonical, canonical + b"\n"):
        raise ImageVerificationError(
            "expected manifest must use canonical JSON with optional final LF"
        )
    with (
        _regular_input(archive_path, MAX_ARCHIVE_BYTES) as stream,
        tarfile.open(fileobj=_TarReadGuard(stream), mode="r:") as container,
    ):
        archive = _OCIArchive(container)
        layout = _json_object(
            archive.named_bytes("oci-layout", MAX_METADATA_BYTES), "OCI layout"
        )
        if layout != {"imageLayoutVersion": "1.0.0"}:
            raise ImageVerificationError("unsupported OCI layout version")
        raw_index = archive.named_bytes("index.json", MAX_METADATA_BYTES)
        index = _json_object(raw_index, "OCI index")
        image, attestations = _runnable(archive, archive.manifests(index))
        descriptor, config, layers, diff_ids = image
        _check_packaged_manifest(
            _verified_final_layer(archive, layers, diff_ids), expected
        )
        roots = _descriptor_list(index["manifests"], "OCI index")
        index_digest = (
            roots[0].digest
            if len(roots) == 1 and roots[0].media_type == OCI_INDEX
            else _sha256(raw_index)
        )
    return {
        "schema_version": 1,
        "domain": "iwe.release-control.image-preparation.v1",
        "layout_index_digest": _sha256(raw_index),
        "index_digest": index_digest,
        "manifest_digest": descriptor.digest,
        "config_digest": config.digest,
        "embedded_manifest_hash": manifest.manifest_hash,
        "release_id": manifest.release_id,
        "source_commit": manifest.source_commit,
        "source_tree": manifest.source_tree,
        "platform": {"os": "linux", "architecture": "amd64"},
        "layer_count": len(layers),
        "attestation_count": attestations,
        "preparation_only": True,
        "signature_verified": False,
        "mutation_authorized": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--embedded-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_release_image(args.archive, args.embedded_manifest)
    except (OSError, ValueError, tarfile.TarError, EOFError, RecursionError) as exc:
        # Print controlled diagnostics, without echoing input content or paths.
        reason = str(exc) if type(exc) is ImageVerificationError else type(exc).__name__
        print(f"release-image=failed ({reason})", file=sys.stderr)
        return 2
    print(_schema.canonical_json_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
