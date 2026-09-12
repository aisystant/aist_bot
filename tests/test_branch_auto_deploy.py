"""Source observations must never become unsupported release authorization."""

import json
import subprocess
from datetime import datetime

import pytest

from scripts import check_branch_auto_deploy as observer

TARGET = observer.Target(
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
)
DEPLOYMENT_ID = "44444444-4444-4444-8444-444444444444"
DIGEST = "sha256:" + "a" * 64
IMAGE = "ghcr.io/example/bot@" + DIGEST


def response():
    return {
        "data": {
            "environment": {
                "id": TARGET.environment_id,
                "projectId": TARGET.project_id,
                "configEtag": "opaque-config-revision-1",
            },
            "serviceInstance": {
                "serviceId": TARGET.service_id,
                "environmentId": TARGET.environment_id,
                "source": {"image": IMAGE, "repo": None},
                "activeDeployments": [
                    {
                        **TARGET.variables(),
                        "id": DEPLOYMENT_ID,
                        "status": "SUCCESS",
                    }
                ],
            },
            "serviceInstanceAutoDeployStatus": {"enabled": False},
            "deploymentTriggers": {
                "pageInfo": {"hasNextPage": False},
                "edges": [],
            },
        }
    }


def arguments():
    return [
        "--project",
        TARGET.project_id,
        "--environment",
        TARGET.environment_id,
        "--service",
        TARGET.service_id,
    ]


def invoke_fixture(tmp_path, capsys, payload):
    fixture = tmp_path / "response.json"
    fixture.write_text(json.dumps(payload))
    code = observer.main([*arguments(), "--input", str(fixture)])
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def no_subprocess(*args, **kwargs):
    pytest.fail("offline observation must not start a subprocess")


def test_offline_pin_is_configuration_evidence_only(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(observer.subprocess, "run", no_subprocess)
    code, report = invoke_fixture(tmp_path, capsys, response())
    assert code == 1
    assert report["evidence_status"] == "available"
    assert report["configured_source_ready"] is True
    assert report["configured_source"]["configured_registry_digest"] == DIGEST
    assert report["active_deployments"] == [{"id": DEPLOYMENT_ID, "status": "SUCCESS"}]
    assert report["observation_mode"] == "offline_fixture"
    assert report["observed_at"] is None
    assert report["readiness"] == "not_ready"
    assert report["image_auto_updates"] == "unknown"
    assert report["runtime_artifact_verified"] is False
    assert report["single_writer_ready"] is False
    assert "image_auto_updates_unverified" in report["blockers"]


def test_live_uses_only_the_selected_read_only_query(capsys, monkeypatch):
    calls = []

    def fake_run(command, **options):
        calls.append(command)
        assert command[:3] == ["railway", "api", observer.QUERY]
        assert json.loads(command[4]) == TARGET.variables()
        assert command[3] == "--variables"
        assert options == {"capture_output": True, "check": True, "timeout": 30}
        assert "mutation" not in command[2]
        assert not any(
            name in command[2].split()
            for name in ("meta", "config", "variables", "logs", "registryCredentials")
        )
        return subprocess.CompletedProcess(command, 0, json.dumps(response()).encode())

    monkeypatch.setattr(observer.subprocess, "run", fake_run)
    assert observer.main([*arguments(), "--live"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert report["observation_mode"] == "live_read_only"
    assert (
        datetime.fromisoformat(report["observed_at"]).utcoffset().total_seconds() == 0
    )
    assert report["runtime_artifact_verified"] is False


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/example/bot:latest",
        "ghcr.io/example/bot:" + "a" * 40,
        "ghcr.io/example/bot@sha256:abc123",
        "example/bot@" + DIGEST,
        "ghcr.io/example/bot@sha256:" + "A" * 64,
    ],
)
def test_tags_and_incomplete_pins_never_supply_digest(image):
    payload = response()
    payload["data"]["serviceInstance"]["source"]["image"] = image
    report = observer.observe(payload, TARGET)
    assert report["configured_source_ready"] is False
    assert report["configured_source"]["configured_registry_digest"] is None
    assert "registry_digest_not_pinned" in report["blockers"]


def test_tag_plus_digest_still_pins_the_registry_artifact():
    assert (
        observer.configured_registry_digest("ghcr.io/example/bot:release-1@" + DIGEST)
        == DIGEST
    )


def test_repository_connection_without_trigger_is_not_image_promotion():
    payload = response()
    payload["data"]["serviceInstance"]["source"] = {
        "image": None,
        "repo": "example/bot",
    }
    report = observer.observe(payload, TARGET)
    assert report["configured_source_ready"] is False
    assert "repository_source_configured" in report["blockers"]
    assert report["branch_auto_deploy"]["enabled"] is False


@pytest.mark.parametrize("enabled, add_trigger", [(True, False), (False, True)])
def test_either_old_deploy_channel_blocks_source_readiness(enabled, add_trigger):
    payload = response()
    payload["data"]["serviceInstanceAutoDeployStatus"]["enabled"] = enabled
    if add_trigger:
        payload["data"]["deploymentTriggers"]["edges"] = [
            {
                "node": {
                    **TARGET.variables(),
                    "id": "trigger-1",
                    "branch": "pilot",
                    "repository": "example/bot",
                    "provider": "github",
                }
            }
        ]
    report = observer.observe(payload, TARGET)
    assert report["configured_source_ready"] is False
    reason = (
        "branch_auto_deploy_enabled"
        if enabled
        else "branch_deployment_triggers_present"
    )
    assert reason in report["blockers"]


@pytest.mark.parametrize("status", ["SKIPPED", "FAILED", "CRASHED", "DEPLOYING"])
def test_nonrunning_deployment_is_gated_even_with_configured_digest(status):
    payload = response()
    payload["data"]["serviceInstance"]["activeDeployments"][0]["status"] = status
    report = observer.observe(payload, TARGET)
    assert "active_deployment_not_successful" in report["blockers"]
    assert report["runtime_artifact_verified"] is False


def test_skipped_build_and_unrequested_metadata_cannot_be_runtime_proof(
    tmp_path, capsys
):
    payload = response()
    payload["data"]["serviceInstance"]["activeDeployments"][0]["meta"] = {
        "imageDigest": DIGEST,
        "skippedBuild": True,
        "variables": "private-sentinel",
    }
    code, report = invoke_fixture(tmp_path, capsys, payload)
    assert code == 1
    assert "private-sentinel" not in json.dumps(report)
    assert "imageDigest" not in json.dumps(report)
    assert report["runtime_artifact_verified"] is False
    assert "runtime_artifact_unverified" in report["blockers"]


@pytest.mark.parametrize("count", [0, 2])
def test_empty_or_overlapping_active_deployments_do_not_pass(count):
    payload = response()
    active = payload["data"]["serviceInstance"]["activeDeployments"]
    active[:] = [{**active[0], "id": f"deployment-{n}"} for n in range(count)]
    report = observer.observe(payload, TARGET)
    assert "active_deployment_not_unique" in report["blockers"]


@pytest.mark.parametrize("field", ["projectId", "environmentId", "serviceId"])
def test_deployment_from_another_target_is_unavailable(field):
    payload = response()
    payload["data"]["serviceInstance"]["activeDeployments"][0][field] = "wrong-target"
    with pytest.raises(observer.EvidenceUnavailable, match="target_identity_mismatch"):
        observer.observe(payload, TARGET)


@pytest.mark.parametrize(
    "path",
    [
        ("environment",),
        ("environment", "configEtag"),
        ("environment", "projectId"),
        ("serviceInstance",),
        ("serviceInstance", "environmentId"),
        ("serviceInstance", "source"),
        ("serviceInstance", "activeDeployments"),
        ("serviceInstanceAutoDeployStatus", "enabled"),
        ("deploymentTriggers", "edges"),
        ("deploymentTriggers", "pageInfo"),
    ],
)
@pytest.mark.parametrize("remove", [False, True])
def test_missing_or_null_required_evidence_fails_closed(tmp_path, capsys, path, remove):
    payload = response()
    node = payload["data"]
    for key in path[:-1]:
        node = node[key]
    if remove:
        del node[path[-1]]
    else:
        node[path[-1]] = None
    code, report = invoke_fixture(tmp_path, capsys, payload)
    assert code == 2
    assert report["readiness"] == "evidence_unavailable"
    assert report["configured_source_ready"] is False


@pytest.mark.parametrize("value", ["false", 0, [], {}])
def test_boolean_coercion_cannot_disable_auto_deploy(value):
    payload = response()
    payload["data"]["serviceInstanceAutoDeployStatus"]["enabled"] = value
    with pytest.raises(observer.EvidenceUnavailable, match="invalid_boolean"):
        observer.observe(payload, TARGET)


def test_incomplete_trigger_page_cannot_hide_a_push_trigger():
    payload = response()
    payload["data"]["deploymentTriggers"]["pageInfo"]["hasNextPage"] = True
    with pytest.raises(
        observer.EvidenceUnavailable, match="incomplete_trigger_listing"
    ):
        observer.observe(payload, TARGET)


def test_api_unsupported_and_partial_data_are_not_silently_accepted(tmp_path, capsys):
    payload = response()
    payload["errors"] = [{"message": "Unsupported field; private-sentinel"}]
    code, report = invoke_fixture(tmp_path, capsys, payload)
    assert code == 2
    assert report["blockers"] == ["railway_api_errors"]
    assert "private-sentinel" not in json.dumps(report)


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("private-sentinel"),
        subprocess.CalledProcessError(1, "railway", stderr="private-sentinel"),
        subprocess.TimeoutExpired("railway", 30, output="private-sentinel"),
    ],
)
def test_cli_failures_never_print_private_diagnostics(capsys, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(observer.subprocess, "run", fail)
    assert observer.main([*arguments(), "--live"]) == 2
    captured = capsys.readouterr()
    assert "private-sentinel" not in captured.out + captured.err
    assert json.loads(captured.out)["blockers"] == ["railway_read_failed"]


@pytest.mark.parametrize("raw", [b"not-json private-sentinel", b"\xff", b"[[]]"])
def test_invalid_fixture_is_unavailable_without_raw_echo(tmp_path, capsys, raw):
    path = tmp_path / "invalid.json"
    path.write_bytes(raw)
    assert observer.main([*arguments(), "--input", str(path)]) == 2
    captured = capsys.readouterr()
    assert "private-sentinel" not in captured.out + captured.err
    assert json.loads(captured.out)["evidence_status"] == "unavailable"


def test_missing_file_is_unavailable(tmp_path, capsys):
    assert observer.main([*arguments(), "--input", str(tmp_path / "missing.json")]) == 2
    assert json.loads(capsys.readouterr().out)["blockers"] == ["fixture_unavailable"]


def test_mode_and_target_must_be_explicit_before_subprocess(monkeypatch):
    monkeypatch.setattr(observer.subprocess, "run", no_subprocess)
    for args in (
        ["--live"],
        arguments(),
        [*arguments(), "--live", "--input", "fixture"],
    ):
        with pytest.raises(SystemExit) as error:
            observer.main(args)
        assert error.value.code == 2
