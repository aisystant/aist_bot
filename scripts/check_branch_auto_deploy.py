#!/usr/bin/env python3
"""Report whether Railway's branch-based auto-deploy is still live.

Read-only diagnostic for a human deciding whether to enable WP-562 cutover.
Queries Railway's `deploymentTriggers` directly (existence of a trigger row
is the actual push-to-deploy channel; a connected `source.repo` alone is not
enough — Railway keeps that reference even when the trigger was deleted).

This script produces NO evidence record and is not wired into any gate. It
answers one question for a human: is the old auto-deploy channel disabled on
both environments right now? The decision to flip `cutover.enabled` stays
manual (WP-562, peer-session 2026-09-04-19-wp562-negative-matrix-evidence).

Requires the `railway` CLI, authenticated (`railway whoami`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass

_PROJECT_ID = "10994bea-dfc6-4883-b0c0-5325a1f51249"
_ENVIRONMENT_ID = "c45eed5e-0250-4c5d-b61c-b0f3862150ff"
_SERVICES = {
    "aist_pilot_bot": "5b3adb5c-391b-474d-8725-d59f7299914b",
    "aist_me_bot": "e840eab0-434f-428f-925c-02fa3a9366d9",
}

_QUERY = """
query($projectId: String!, $environmentId: String!, $serviceId: String!) {
  deploymentTriggers(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId) {
    edges { node { branch repository provider } }
  }
}
"""


@dataclass(frozen=True)
class TriggerStatus:
    service: str
    triggers: tuple[dict[str, str], ...]

    @property
    def auto_deploy_live(self) -> bool:
        return len(self.triggers) > 0


def _query_deployment_triggers(service_id: str) -> list[dict[str, str]]:
    variables = {
        "projectId": _PROJECT_ID,
        "environmentId": _ENVIRONMENT_ID,
        "serviceId": service_id,
    }
    result = subprocess.run(
        ["railway", "api", _QUERY, "--variables", json.dumps(variables)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)
    if "errors" in payload:
        raise RuntimeError(f"Railway API returned errors: {payload['errors']}")
    edges = payload["data"]["deploymentTriggers"]["edges"]
    return [edge["node"] for edge in edges]


def check_all() -> list[TriggerStatus]:
    return [
        TriggerStatus(service=name, triggers=tuple(_query_deployment_triggers(service_id)))
        for name, service_id in _SERVICES.items()
    ]


def format_report(statuses: list[TriggerStatus]) -> str:
    lines = [
        "Проверка старого канала выпуска (branch-based auto-deploy, Railway)",
        "Это НЕ проверка «можно ли задеплоить вообще» — ручной выпуск через",
        "консоль/CLI остаётся возможен независимо от этого триггера.",
        "",
    ]
    for status in statuses:
        if status.auto_deploy_live:
            # Railway allows at most one trigger per service+environment;
            # --json exposes the full list if that ever changes.
            trigger = status.triggers[0]
            lines.append(
                f"{status.service}: branch-based auto-deploy LIVE "
                f"→ branch={trigger['branch']!r} repo={trigger['repository']!r} "
                f"provider={trigger['provider']!r}"
            )
        else:
            lines.append(
                f"{status.service}: branch-based auto-deploy NONE "
                "(ручной выпуск через CLI/UI всё ещё возможен)"
            )
    all_disabled = all(not s.auto_deploy_live for s in statuses)
    lines.append("")
    lines.append(
        "Вывод: "
        + (
            "старый канал отключён на обеих средах."
            if all_disabled
            else "старый канал ещё жив хотя бы на одной среде — "
            "cutover.enabled должен оставаться выключенным."
        )
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="print raw JSON instead of the report"
    )
    args = parser.parse_args(argv)

    try:
        statuses = check_all()
    except FileNotFoundError:
        print("railway CLI not found — is it installed and on PATH?", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"railway api failed: {exc.stderr}", file=sys.stderr)
        return 2
    except (
        subprocess.TimeoutExpired,
        RuntimeError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"could not read deployment triggers: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    s.service: {
                        "auto_deploy_live": s.auto_deploy_live,
                        "triggers": list(s.triggers),
                    }
                    for s in statuses
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(format_report(statuses))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
