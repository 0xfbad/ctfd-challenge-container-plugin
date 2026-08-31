from __future__ import annotations

import json
import os
import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

VOLUME_POLICY_ENV = "CHALLENGE_CONTAINERS_VOLUME_POLICY_JSON"
VOLUME_POLICY_SCHEMA_VERSION = 1
MOUNT_SCHEMA_VERSION = 1

POLICY_ID_LABEL = "org.ctfd.challenge-containers.volume-policy"
POLICY_REVISION_LABEL = "org.ctfd.challenge-containers.volume-policy-revision"
LOGICAL_NAME_LABEL = "org.ctfd.challenge-containers.logical-volume"

__all__ = [
    "LOGICAL_NAME_LABEL",
    "MOUNT_SCHEMA_VERSION",
    "POLICY_ID_LABEL",
    "POLICY_REVISION_LABEL",
    "VOLUME_POLICY_ENV",
    "VOLUME_POLICY_SCHEMA_VERSION",
    "ContextReadiness",
    "MountConfigError",
    "MountRequest",
    "ReadinessIssue",
    "ReadinessReport",
    "VolumeMetadata",
    "VolumePolicy",
    "VolumePolicyError",
    "canonical_mount_config",
    "docker_volume_metadata",
    "evaluate_volume_readiness",
    "load_volume_policy",
    "parse_mount_config",
    "parse_volume_policy",
    "required_volume_labels",
    "resolve_mounts_for_context",
]

_MAX_POLICY_BYTES = 65_536
_MAX_MOUNT_CONFIG_BYTES = 16_384
_MAX_CONTEXTS = 128
_MAX_VOLUMES_PER_CONTEXT = 128
_MAX_TARGETS_PER_VOLUME = 16
_MAX_MOUNTS = 16

_LOGICAL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_DOCKER_VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_CONTEXT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_POLICY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

# A data volume never needs to cover a kernel pseudo-filesystem, daemon socket
# location, or the entire container filesystem. Reject these even if an
# infrastructure policy accidentally lists one.
_FORBIDDEN_TARGET_ROOTS = (
    "/dev",
    "/proc",
    "/run",
    "/sys",
    "/var/run",
)

MountScope = Literal["entry", "service"]


class VolumePolicyError(ValueError):
    """The environment-owned infrastructure policy is invalid."""


class MountConfigError(ValueError):
    """A challenge mount declaration is invalid or is not permitted."""


@dataclass(frozen=True)
class VolumeRule:
    logical_name: str
    docker_name: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class ContextVolumePolicy:
    context_name: str
    volumes: Mapping[str, VolumeRule]


@dataclass(frozen=True)
class VolumePolicy:
    policy_id: str
    revision: int
    contexts: Mapping[str, ContextVolumePolicy]

    @classmethod
    def disabled(cls) -> VolumePolicy:
        return cls(policy_id="disabled", revision=0, contexts={})


@dataclass(frozen=True)
class MountRequest:
    logical_name: str
    target: str
    scope: MountScope


@dataclass(frozen=True)
class VolumeMetadata:
    name: str
    driver: str
    labels: Mapping[str, str]
    has_driver_options: bool


@dataclass(frozen=True)
class ReadinessIssue:
    context_name: str
    logical_name: str | None
    code: str


@dataclass(frozen=True)
class ContextReadiness:
    context_name: str
    ready: bool
    issues: tuple[ReadinessIssue, ...]


@dataclass(frozen=True)
class ReadinessReport:
    contexts: tuple[ContextReadiness, ...]

    @property
    def eligible_contexts(self) -> tuple[str, ...]:
        return tuple(item.context_name for item in self.contexts if item.ready)


class DockerVolumeCollection(Protocol):
    def get(self, _volume_id: str) -> Any: ...


class DockerClientWithVolumes(Protocol):
    volumes: DockerVolumeCollection


VolumeLookup = Callable[[str, str], VolumeMetadata | None]


def _require_object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{description} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{description} keys must be strings")
    return value


def _require_exact_keys(
    value: Mapping[str, object], *, required: set[str], optional: set[str] | None = None, description: str
) -> None:
    optional = optional or set()
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ValueError(f"{description} is missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{description} has unsupported fields: {', '.join(sorted(unknown))}")


def _parse_json(raw: str, *, max_bytes: int, description: str) -> object:
    if len(raw.encode("utf-8")) > max_bytes:
        raise ValueError(f"{description} is too large")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{description} is not valid JSON") from exc


def _validate_context_name(value: object) -> str:
    if not isinstance(value, str) or not _CONTEXT_NAME_RE.fullmatch(value):
        raise VolumePolicyError("volume policy context names must be 1-128 safe characters")
    return value


def _validate_logical_name(value: object, *, error_cls: type[ValueError] = VolumePolicyError) -> str:
    if not isinstance(value, str) or not _LOGICAL_NAME_RE.fullmatch(value):
        raise error_cls("logical volume names must be 1-64 lowercase safe characters")
    return value


def _validate_target(value: object, *, error_cls: type[ValueError]) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise error_cls("volume targets must be nonempty strings no longer than 512 characters")
    if (
        not value.startswith("/")
        or value.startswith("//")
        or posixpath.normpath(value) != value
        or value == "/"
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise error_cls("volume targets must be normalized absolute container paths other than /")
    for root in _FORBIDDEN_TARGET_ROOTS:
        if value == root or value.startswith(root + "/"):
            raise error_cls(f"volume targets under {root} are not allowed")
    return value


def parse_volume_policy(raw: str | None) -> VolumePolicy:
    """Parse the infrastructure-owned policy.

    Schema::

        {
          "schema_version": 1,
          "policy_id": "event-2026",
          "revision": 3,
          "contexts": {
            "local": {
              "volumes": {
                "web-assets": {
                  "docker_name": "ctfd-web-assets-v3",
                  "targets": ["/opt/challenge/assets"]
                }
              }
            }
          }
        }

    Docker volumes must be pre-created using the fixed labels derived from
    policy_id, revision, and the logical volume name. Driver selection/options
    are deliberately not configurable here.
    """

    if raw is None or not raw.strip():
        return VolumePolicy.disabled()

    try:
        root = _require_object(
            _parse_json(raw, max_bytes=_MAX_POLICY_BYTES, description="volume policy"), "volume policy"
        )
        _require_exact_keys(
            root,
            required={"schema_version", "policy_id", "revision", "contexts"},
            description="volume policy",
        )

        schema_version = root["schema_version"]
        if type(schema_version) is not int or schema_version != VOLUME_POLICY_SCHEMA_VERSION:
            raise VolumePolicyError(f"volume policy schema_version must be {VOLUME_POLICY_SCHEMA_VERSION}")

        policy_id = root["policy_id"]
        if not isinstance(policy_id, str) or not _POLICY_ID_RE.fullmatch(policy_id):
            raise VolumePolicyError("volume policy_id must be 1-128 safe characters")

        revision = root["revision"]
        if type(revision) is not int or not 1 <= revision <= 2_147_483_647:
            raise VolumePolicyError("volume policy revision must be a positive 32-bit integer")

        contexts_raw = _require_object(root["contexts"], "volume policy contexts")
        if len(contexts_raw) > _MAX_CONTEXTS:
            raise VolumePolicyError(f"volume policy supports at most {_MAX_CONTEXTS} contexts")

        contexts: dict[str, ContextVolumePolicy] = {}
        for untrusted_context_name, context_value in contexts_raw.items():
            context_name = _validate_context_name(untrusted_context_name)
            context_obj = _require_object(context_value, f"volume policy context {context_name}")
            _require_exact_keys(context_obj, required={"volumes"}, description=f"volume policy context {context_name}")
            volumes_raw = _require_object(context_obj["volumes"], f"volume policy context {context_name} volumes")
            if len(volumes_raw) > _MAX_VOLUMES_PER_CONTEXT:
                raise VolumePolicyError(
                    f"volume policy context {context_name} supports at most {_MAX_VOLUMES_PER_CONTEXT} volumes"
                )

            volumes: dict[str, VolumeRule] = {}
            docker_names: set[str] = set()
            for untrusted_logical_name, volume_value in volumes_raw.items():
                logical_name = _validate_logical_name(untrusted_logical_name)
                volume_obj = _require_object(volume_value, f"logical volume {logical_name}")
                _require_exact_keys(
                    volume_obj, required={"docker_name", "targets"}, description=f"logical volume {logical_name}"
                )

                docker_name = volume_obj["docker_name"]
                if not isinstance(docker_name, str) or not _DOCKER_VOLUME_NAME_RE.fullmatch(docker_name):
                    raise VolumePolicyError(
                        f"logical volume {logical_name} docker_name must be a valid named-volume identifier"
                    )
                if docker_name in docker_names:
                    raise VolumePolicyError(f"Docker volume {docker_name} is assigned more than once in {context_name}")
                docker_names.add(docker_name)

                targets_raw = volume_obj["targets"]
                if not isinstance(targets_raw, list) or not targets_raw:
                    raise VolumePolicyError(f"logical volume {logical_name} targets must be a nonempty array")
                if len(targets_raw) > _MAX_TARGETS_PER_VOLUME:
                    raise VolumePolicyError(
                        f"logical volume {logical_name} supports at most {_MAX_TARGETS_PER_VOLUME} targets"
                    )
                targets = tuple(_validate_target(target, error_cls=VolumePolicyError) for target in targets_raw)
                if len(set(targets)) != len(targets):
                    raise VolumePolicyError(f"logical volume {logical_name} contains duplicate targets")

                volumes[logical_name] = VolumeRule(logical_name=logical_name, docker_name=docker_name, targets=targets)

            contexts[context_name] = ContextVolumePolicy(context_name=context_name, volumes=volumes)

        return VolumePolicy(policy_id=policy_id, revision=revision, contexts=contexts)
    except VolumePolicyError:
        raise
    except (TypeError, ValueError) as exc:
        raise VolumePolicyError(str(exc)) from exc


def load_volume_policy(environ: Mapping[str, str] | None = None) -> VolumePolicy:
    source = os.environ if environ is None else environ
    return parse_volume_policy(source.get(VOLUME_POLICY_ENV))


def parse_mount_config(raw: str | object | None, *, expected_scope: MountScope) -> tuple[MountRequest, ...]:
    """Validate the versioned challenge mount format.

    Entry and service declarations use the same schema; callers must provide
    the scope they are reading from. All mounts are named volumes and strictly
    read-only. Host paths, driver options, shorthand strings, and implicit
    service inheritance are never accepted.
    """

    if expected_scope not in ("entry", "service"):
        raise MountConfigError("mount scope must be entry or service")
    if raw is None or raw == "":
        return ()

    try:
        parsed = (
            _parse_json(raw, max_bytes=_MAX_MOUNT_CONFIG_BYTES, description="mount configuration")
            if isinstance(raw, str)
            else raw
        )
        root = _require_object(parsed, "mount configuration")

        if "schema_version" not in root or "mounts" not in root or "scope" not in root:
            raise MountConfigError("mount configuration must use the versioned logical-volume format")

        _require_exact_keys(root, required={"schema_version", "scope", "mounts"}, description="mount configuration")
        schema_version = root["schema_version"]
        if type(schema_version) is not int or schema_version != MOUNT_SCHEMA_VERSION:
            raise MountConfigError(f"mount schema_version must be {MOUNT_SCHEMA_VERSION}")
        scope = root["scope"]
        if scope != expected_scope:
            raise MountConfigError(f"{expected_scope} mounts must declare scope={expected_scope}")

        mounts_raw = root["mounts"]
        if not isinstance(mounts_raw, list):
            raise MountConfigError("mounts must be an array")
        if len(mounts_raw) > _MAX_MOUNTS:
            raise MountConfigError(f"at most {_MAX_MOUNTS} mounts are allowed")

        mounts: list[MountRequest] = []
        logical_names: set[str] = set()
        targets: set[str] = set()
        for index, mount_value in enumerate(mounts_raw):
            mount = _require_object(mount_value, f"mount {index}")
            _require_exact_keys(
                mount,
                required={"type", "name", "target", "read_only"},
                description=f"mount {index}",
            )
            if mount["type"] != "volume":
                raise MountConfigError(f"mount {index} type must be volume")
            if mount["read_only"] is not True:
                raise MountConfigError(f"mount {index} must set read_only to true")
            logical_name = _validate_logical_name(mount["name"], error_cls=MountConfigError)
            target = _validate_target(mount["target"], error_cls=MountConfigError)
            if logical_name in logical_names:
                raise MountConfigError(f"logical volume {logical_name} may only be mounted once")
            if target in targets:
                raise MountConfigError(f"container target {target} may only be mounted once")
            logical_names.add(logical_name)
            targets.add(target)
            mounts.append(MountRequest(logical_name=logical_name, target=target, scope=expected_scope))

        return tuple(mounts)
    except MountConfigError:
        raise
    except (TypeError, ValueError) as exc:
        raise MountConfigError(str(exc)) from exc


def resolve_mounts_for_context(
    policy: VolumePolicy, context_name: str, mounts: Sequence[MountRequest]
) -> dict[str, dict[str, str]]:
    """Resolve logical requests to Docker SDK volume arguments for one context."""

    if not mounts:
        return {}
    context = policy.contexts.get(context_name)
    if context is None:
        raise MountConfigError(f"context {context_name} has no named-volume policy")

    resolved: dict[str, dict[str, str]] = {}
    for mount in mounts:
        rule = context.volumes.get(mount.logical_name)
        if rule is None:
            raise MountConfigError(f"logical volume {mount.logical_name} is not allowed on context {context_name}")
        if mount.target not in rule.targets:
            raise MountConfigError(
                f"target {mount.target} is not allowed for logical volume {mount.logical_name} on context {context_name}"
            )
        if rule.docker_name in resolved:
            raise MountConfigError(f"logical volume {mount.logical_name} resolves to a duplicate Docker volume")
        resolved[rule.docker_name] = {"bind": mount.target, "mode": "ro"}
    return resolved


def required_volume_labels(policy: VolumePolicy, logical_name: str) -> dict[str, str]:
    """Return the exact non-secret labels required on a provisioned volume."""

    normalized_name = _validate_logical_name(logical_name, error_cls=MountConfigError)
    if policy.revision <= 0:
        raise MountConfigError("named-volume policy is disabled")
    return {
        POLICY_ID_LABEL: policy.policy_id,
        POLICY_REVISION_LABEL: str(policy.revision),
        LOGICAL_NAME_LABEL: normalized_name,
    }


def docker_volume_metadata(client: DockerClientWithVolumes, docker_name: str) -> VolumeMetadata | None:
    """Inspect one exact Docker volume without invoking create-on-missing behavior."""

    try:
        volume = client.volumes.get(docker_name)
    except Exception as exc:
        # Avoid importing Docker solely for its NotFound class in this policy
        # module. Only a genuine 404 is treated as absence; connection and
        # authorization failures must remain distinguishable to callers.
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status_code == 404 or getattr(response, "status_code", None) == 404:
            return None
        raise

    attrs = getattr(volume, "attrs", None)
    if not isinstance(attrs, dict):
        return VolumeMetadata(name="", driver="", labels={}, has_driver_options=True)
    labels_raw = attrs.get("Labels")
    labels = {str(key): str(value) for key, value in labels_raw.items()} if isinstance(labels_raw, dict) else {}
    options_raw = attrs.get("Options")
    has_driver_options = bool(options_raw) or (options_raw is not None and not isinstance(options_raw, dict))
    return VolumeMetadata(
        name=str(attrs.get("Name", "")),
        driver=str(attrs.get("Driver", "")),
        labels=labels,
        has_driver_options=has_driver_options,
    )


def _volume_issues(
    policy: VolumePolicy,
    context_name: str,
    rule: VolumeRule,
    metadata: VolumeMetadata | None,
) -> list[ReadinessIssue]:
    def issue(code: str) -> ReadinessIssue:
        return ReadinessIssue(context_name=context_name, logical_name=rule.logical_name, code=code)

    if metadata is None:
        return [issue("volume_missing")]
    issues: list[ReadinessIssue] = []
    if metadata.name != rule.docker_name:
        issues.append(issue("volume_identity_mismatch"))
    if metadata.driver != "local":
        issues.append(issue("volume_driver_not_allowed"))
    if metadata.has_driver_options:
        issues.append(issue("volume_driver_options_not_allowed"))
    required_labels = required_volume_labels(policy, rule.logical_name)
    for key, expected in required_labels.items():
        if metadata.labels.get(key) != expected:
            issues.append(issue("volume_label_mismatch"))
            break
    return issues


def evaluate_volume_readiness(
    policy: VolumePolicy,
    context_names: Sequence[str],
    mounts: Sequence[MountRequest],
    lookup: VolumeLookup,
) -> ReadinessReport:
    """Return eligible contexts and value-free readiness diagnostics.

    ``lookup`` must perform an exact inspection and return ``None`` only for a
    confirmed missing volume. It must not call create. Infrastructure errors
    are represented as unavailable without including exception text, endpoint
    details, driver options, labels, or other potentially sensitive values.
    """

    reports: list[ContextReadiness] = []
    for context_name in dict.fromkeys(context_names):
        issues: list[ReadinessIssue] = []
        if not mounts:
            reports.append(ContextReadiness(context_name=context_name, ready=True, issues=()))
            continue

        context = policy.contexts.get(context_name)
        if context is None:
            issues.append(ReadinessIssue(context_name, None, "context_policy_missing"))
        else:
            seen_docker_names: set[str] = set()
            for mount in mounts:
                rule = context.volumes.get(mount.logical_name)
                if rule is None:
                    issues.append(ReadinessIssue(context_name, mount.logical_name, "logical_volume_not_allowed"))
                    continue
                if mount.target not in rule.targets:
                    issues.append(ReadinessIssue(context_name, mount.logical_name, "target_not_allowed"))
                    continue
                if rule.docker_name in seen_docker_names:
                    issues.append(ReadinessIssue(context_name, mount.logical_name, "duplicate_resolved_volume"))
                    continue
                seen_docker_names.add(rule.docker_name)
                try:
                    metadata = lookup(context_name, rule.docker_name)
                # Lookup adapters may surface Docker, SSH, or transport-specific
                # exceptions. Diagnostics intentionally collapse all of them so
                # endpoint and credential details cannot escape.
                except Exception:  # noqa: BLE001
                    issues.append(ReadinessIssue(context_name, mount.logical_name, "volume_inspection_unavailable"))
                    continue
                issues.extend(_volume_issues(policy, context_name, rule, metadata))

        reports.append(ContextReadiness(context_name=context_name, ready=not issues, issues=tuple(issues)))
    return ReadinessReport(contexts=tuple(reports))


def canonical_mount_config(mounts: Sequence[MountRequest], *, scope: MountScope) -> str:
    """Serialize validated mounts."""

    if scope not in ("entry", "service") or any(mount.scope != scope for mount in mounts):
        raise MountConfigError("all mounts must match the requested scope")
    value = {
        "schema_version": MOUNT_SCHEMA_VERSION,
        "scope": scope,
        "mounts": [
            {
                "type": "volume",
                "name": mount.logical_name,
                "target": mount.target,
                "read_only": True,
            }
            for mount in mounts
        ],
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
