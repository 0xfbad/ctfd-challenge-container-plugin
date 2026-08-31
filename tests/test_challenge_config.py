import json

import pytest

from challenge_config import normalize_challenge_fields, normalize_network, normalize_services
from utils import ValidationError


@pytest.mark.parametrize(
    ("field", "value"),
    [("port", 0), ("port", 65536), ("max_memory_mb", 5), ("max_cpu", "nan"), ("max_cpu", 0)],
)
def test_resource_and_port_bounds(field, value):
    with pytest.raises(ValidationError):
        normalize_challenge_fields({field: value})


def test_service_resources_are_canonical_and_bounded():
    encoded, services = normalize_services({"db": {"image": "postgres:17", "max_memory_mb": "256", "max_cpu": "0.5"}})
    assert encoded == json.dumps(services, sort_keys=True, separators=(",", ":"))
    assert services["db"]["max_memory_mb"] == 256
    assert services["db"]["max_cpu"] == 0.5


def test_network_rejects_duplicate_or_out_of_subnet_addresses():
    with pytest.raises(ValidationError, match="duplicate"):
        normalize_network(
            {"subnet": "10.20.0.0/24", "ips": {"entry": "10.20.0.10", "db": "10.20.0.10"}},
            {"db"},
        )
    with pytest.raises(ValidationError, match="usable address"):
        normalize_network({"subnet": "10.20.0.0/24", "ips": {"entry": "10.21.0.10"}}, {"db"})


def test_unknown_stack_fields_and_capabilities_are_rejected():
    with pytest.raises(ValidationError, match="unsupported fields"):
        normalize_services({"db": {"image": "postgres:17", "privileged": True}})
    with pytest.raises(ValidationError, match="unsupported capability"):
        normalize_services({"db": {"image": "postgres:17", "cap_add": "SYS_ADMIN"}})
