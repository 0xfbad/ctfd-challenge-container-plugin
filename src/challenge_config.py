"""normalizes challenge and stack config from html form strings or json values, before any orm write or docker io"""

from __future__ import annotations

import ipaddress
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from .utils import ValidationError, parse_json_object
from .volume_policy import canonical_mount_config, parse_mount_config

MAX_SERVICES = 20
MAX_TEXT = 4_096
MAX_IMAGE = 512
MAX_MEMORY_MB = 1_048_576
MAX_CPU = 1_024.0
ALLOWED_CTYPES = frozenset({"web", "tcp", "ssh"})
ALLOWED_CAPABILITIES = frozenset({"NET_ADMIN", "NET_RAW", "SYS_PTRACE", "SYS_NICE"})
SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _empty(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _string(value: object, field: str, *, required: bool = False, maximum: int = MAX_TEXT) -> str | None:
    if _empty(value):
        if required:
            raise ValidationError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    value = value.strip()
    if len(value) > maximum or "\x00" in value:
        raise ValidationError(f"{field} must be at most {maximum} characters")
    return value


def _int(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
    nullable: bool = False,
) -> int | None:
    if _empty(value) and nullable:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be an integer")
    if isinstance(value, str):
        text = value.strip()
        if not text or not re.fullmatch(r"[+-]?\d+", text):
            raise ValidationError(f"{field} must be an integer")
        result = int(text)
    elif isinstance(value, int):
        result = value
    else:
        raise ValidationError(f"{field} must be an integer")
    if result < minimum or result > maximum:
        raise ValidationError(f"{field} must be between {minimum} and {maximum}")
    return result


def _float(
    value: object,
    field: str,
    *,
    minimum: float,
    maximum: float,
    nullable: bool = False,
) -> float | None:
    if _empty(value) and nullable:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a number")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a number") from exc
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ValidationError(f"{field} must be between {minimum:g} and {maximum:g}")
    return result


def _capabilities(value: object, field: str = "cap_add") -> str:
    if _empty(value):
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a comma-separated string")
    values = []
    for raw in value.split(","):
        capability = raw.strip().upper()
        if not capability:
            continue
        if capability not in ALLOWED_CAPABILITIES:
            raise ValidationError(f"{field} contains unsupported capability {capability}")
        if capability not in values:
            values.append(capability)
    return ",".join(values)


def _environment(value: object, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field} must be an object")
    if len(value) > 128:
        raise ValidationError(f"{field} must not contain more than 128 variables")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 256 or "\x00" in key:
            raise ValidationError(f"{field} contains an invalid variable name")
        if not isinstance(item, str) or len(item) > MAX_TEXT or "\x00" in item:
            raise ValidationError(f"{field}.{key} must be a string of at most {MAX_TEXT} characters")
        result[key] = item
    return result


def normalize_services(value: object) -> tuple[str | None, dict[str, dict[str, Any]]]:
    if _empty(value):
        return None, {}
    services = parse_json_object(value, "services_json")
    if len(services) > MAX_SERVICES:
        raise ValidationError(f"services_json must not contain more than {MAX_SERVICES} services")

    normalized: dict[str, dict[str, Any]] = {}
    allowed = {"image", "command", "environment", "cap_add", "volumes", "max_memory_mb", "max_cpu"}
    for service_name, raw_config in services.items():
        if not SERVICE_NAME_RE.fullmatch(service_name) or service_name == "entry":
            raise ValidationError(f"invalid companion service name: {service_name}")
        if not isinstance(raw_config, Mapping):
            raise ValidationError(f"service {service_name} must be an object")
        unknown = set(raw_config) - allowed
        if unknown:
            raise ValidationError(
                f"service {service_name} has unsupported fields: {', '.join(sorted(str(key) for key in unknown))}"
            )

        image = _string(
            raw_config.get("image"), f"services_json.{service_name}.image", required=True, maximum=MAX_IMAGE
        )
        config: dict[str, Any] = {"image": image}
        command = _string(raw_config.get("command"), f"services_json.{service_name}.command")
        if command is not None:
            config["command"] = command
        environment = _environment(raw_config.get("environment"), f"services_json.{service_name}.environment")
        if environment:
            config["environment"] = environment
        capabilities = _capabilities(raw_config.get("cap_add"), f"services_json.{service_name}.cap_add")
        if capabilities:
            config["cap_add"] = capabilities
        if "volumes" in raw_config and not _empty(raw_config.get("volumes")):
            mounts = parse_mount_config(raw_config["volumes"], expected_scope="service")
            config["volumes"] = json.loads(canonical_mount_config(mounts, scope="service"))
        memory = _int(
            raw_config.get("max_memory_mb"),
            f"services_json.{service_name}.max_memory_mb",
            minimum=6,
            maximum=MAX_MEMORY_MB,
            nullable=True,
        )
        if memory is not None:
            config["max_memory_mb"] = memory
        cpu = _float(
            raw_config.get("max_cpu"),
            f"services_json.{service_name}.max_cpu",
            minimum=0.01,
            maximum=MAX_CPU,
            nullable=True,
        )
        if cpu is not None:
            config["max_cpu"] = cpu
        normalized[service_name] = config

    return json.dumps(normalized, sort_keys=True, separators=(",", ":")), normalized


def normalize_network(value: object, service_names: set[str]) -> str | None:
    if _empty(value):
        return None
    config = parse_json_object(value, "network_json")
    unknown = set(config) - {"subnet", "ips"}
    if unknown:
        raise ValidationError(f"network_json has unsupported fields: {', '.join(sorted(unknown))}")

    subnet_value = config.get("subnet")
    subnet = None
    if subnet_value is not None:
        if not isinstance(subnet_value, str):
            raise ValidationError("network_json.subnet must be a string")
        try:
            subnet = ipaddress.ip_network(subnet_value, strict=True)
        except ValueError as exc:
            raise ValidationError("network_json.subnet must be a valid canonical subnet") from exc
        if subnet.version != 4:
            raise ValidationError("network_json.subnet must be IPv4")

    raw_ips = config.get("ips", {})
    if not isinstance(raw_ips, Mapping):
        raise ValidationError("network_json.ips must be an object")
    allowed_names = {"entry", *service_names}
    normalized_ips: dict[str, str] = {}
    seen: set[ipaddress.IPv4Address] = set()
    for service_name, raw_ip in raw_ips.items():
        if service_name not in allowed_names:
            raise ValidationError(f"network_json.ips contains unknown service {service_name}")
        if not isinstance(raw_ip, str):
            raise ValidationError(f"network_json.ips.{service_name} must be a string")
        try:
            address = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise ValidationError(f"network_json.ips.{service_name} must be a valid IP address") from exc
        if not isinstance(address, ipaddress.IPv4Address):
            raise ValidationError(f"network_json.ips.{service_name} must be IPv4")
        if subnet is None:
            raise ValidationError("network_json.subnet is required when static IPs are configured")
        if address not in subnet or address in (subnet.network_address, subnet.broadcast_address):
            raise ValidationError(f"network_json.ips.{service_name} must be a usable address in the subnet")
        if address in seen:
            raise ValidationError("network_json.ips contains duplicate addresses")
        seen.add(address)
        normalized_ips[service_name] = str(address)

    normalized: dict[str, object] = {}
    if subnet is not None:
        normalized["subnet"] = str(subnet)
    if normalized_ips:
        normalized["ips"] = normalized_ips
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def normalize_challenge_fields(
    data: Mapping[str, object], *, existing_service_names: set[str] | None = None
) -> dict[str, object]:
    """normalizes only the fields present in the request, absent fields keep their stored values"""
    result = dict(data)
    if "image" in result:
        result["image"] = _string(result["image"], "image", required=True, maximum=MAX_IMAGE)
    if "port" in result:
        result["port"] = _int(result["port"], "port", minimum=1, maximum=65_535)
    if "ctype" in result:
        ctype = _string(result["ctype"], "ctype", required=True, maximum=16)
        if ctype not in ALLOWED_CTYPES:
            raise ValidationError("ctype must be web, tcp, or ssh")
        result["ctype"] = ctype
    for field in ("command", "ssh_username", "ssh_password"):
        if field in result:
            result[field] = _string(result[field], field)
    if "docker_context" in result:
        result["docker_context"] = _string(result["docker_context"], "docker_context", maximum=512)
    if "expiration_seconds" in result:
        result["expiration_seconds"] = _int(
            result["expiration_seconds"], "expiration_seconds", minimum=1, maximum=604_800, nullable=True
        )
    if "max_renewals" in result:
        result["max_renewals"] = _int(result["max_renewals"], "max_renewals", minimum=0, maximum=1_000, nullable=True)
    if "max_memory_mb" in result:
        result["max_memory_mb"] = _int(
            result["max_memory_mb"], "max_memory_mb", minimum=6, maximum=MAX_MEMORY_MB, nullable=True
        )
    if "max_cpu" in result:
        result["max_cpu"] = _float(result["max_cpu"], "max_cpu", minimum=0.01, maximum=MAX_CPU, nullable=True)
    if "cap_add" in result:
        result["cap_add"] = _capabilities(result["cap_add"])
    if "volumes" in result:
        if _empty(result["volumes"]):
            result["volumes"] = ""
        else:
            mounts = parse_mount_config(result["volumes"], expected_scope="entry")
            result["volumes"] = canonical_mount_config(mounts, scope="entry")

    services: dict[str, dict[str, Any]] = {}
    if "services_json" in result:
        services_json, services = normalize_services(result["services_json"])
        result["services_json"] = services_json
    if "network_json" in result:
        # on a network only update the caller supplied names keep unknown static ip keys from slipping through
        service_names = set(services) if "services_json" in result else set(existing_service_names or ())
        result["network_json"] = normalize_network(result["network_json"], service_names)
    return result
