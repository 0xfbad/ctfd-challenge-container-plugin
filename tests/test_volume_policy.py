import json

import pytest

from volume_policy import (
    LOGICAL_NAME_LABEL,
    POLICY_ID_LABEL,
    POLICY_REVISION_LABEL,
    MountConfigError,
    VolumeMetadata,
    evaluate_volume_readiness,
    parse_mount_config,
    parse_volume_policy,
    resolve_mounts_for_context,
)


def _policy():
    return parse_volume_policy(
        json.dumps(
            {
                "schema_version": 1,
                "policy_id": "event-2026",
                "revision": 3,
                "contexts": {
                    "local": {
                        "volumes": {
                            "assets": {
                                "docker_name": "challenge-assets-v3",
                                "targets": ["/opt/challenge/assets"],
                            }
                        }
                    }
                },
            }
        )
    )


def _mounts():
    return parse_mount_config(
        {
            "schema_version": 1,
            "scope": "entry",
            "mounts": [
                {
                    "type": "volume",
                    "name": "assets",
                    "target": "/opt/challenge/assets",
                    "read_only": True,
                }
            ],
        },
        expected_scope="entry",
    )


def test_unversioned_mount_mapping_is_rejected():
    with pytest.raises(MountConfigError, match="versioned"):
        parse_mount_config('{"/host/path":{"bind":"/data","mode":"rw"}}', expected_scope="entry")


@pytest.mark.parametrize("target", ["/", "/proc", "/var/run/docker.sock", "relative/path"])
def test_dangerous_mount_targets_are_rejected(target):
    with pytest.raises(MountConfigError):
        parse_mount_config(
            {
                "schema_version": 1,
                "scope": "entry",
                "mounts": [{"type": "volume", "name": "assets", "target": target, "read_only": True}],
            },
            expected_scope="entry",
        )


def test_writable_mount_is_rejected():
    declaration = json.loads(
        json.dumps(
            {
                "schema_version": 1,
                "scope": "entry",
                "mounts": [
                    {
                        "type": "volume",
                        "name": "assets",
                        "target": "/opt/challenge/assets",
                        "read_only": False,
                    }
                ],
            }
        )
    )
    with pytest.raises(MountConfigError, match="read_only"):
        parse_mount_config(declaration, expected_scope="entry")


def test_exact_preprovisioned_volume_is_resolved_read_only():
    assert resolve_mounts_for_context(_policy(), "local", _mounts()) == {
        "challenge-assets-v3": {"bind": "/opt/challenge/assets", "mode": "ro"}
    }


def test_readiness_rejects_label_revision_and_driver_option_mismatch():
    policy = _policy()
    wrong = VolumeMetadata(
        name="challenge-assets-v3",
        driver="local",
        labels={
            POLICY_ID_LABEL: "event-2026",
            POLICY_REVISION_LABEL: "2",
            LOGICAL_NAME_LABEL: "assets",
        },
        has_driver_options=True,
    )
    report = evaluate_volume_readiness(policy, ["local"], _mounts(), lambda *_args: wrong)
    assert report.eligible_contexts == ()
    assert {issue.code for issue in report.contexts[0].issues} == {
        "volume_driver_options_not_allowed",
        "volume_label_mismatch",
    }


def test_readiness_does_not_turn_inspection_failure_into_missing_volume():
    def unavailable(*_args):
        raise RuntimeError("secret endpoint details")

    report = evaluate_volume_readiness(_policy(), ["local"], _mounts(), unavailable)
    assert report.eligible_contexts == ()
    assert [issue.code for issue in report.contexts[0].issues] == ["volume_inspection_unavailable"]
