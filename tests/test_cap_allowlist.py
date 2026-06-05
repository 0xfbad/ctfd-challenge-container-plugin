import logging
import sys

_cm = sys.modules["_cc_plugin.container_manager"]
_build_caps = _cm._build_caps
_filter_admin_caps = _cm._filter_admin_caps
_ALLOWED_CAPS = _cm._ALLOWED_CAPS
_SSH_CAPS = _cm._SSH_CAPS

_LOGGER_NAME = _cm.logger.name


def test_disallowed_cap_dropped_and_warned(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = _build_caps(None, "SYS_ADMIN", chal_id=42)
    assert "SYS_ADMIN" not in result
    assert result == []
    assert any("SYS_ADMIN" in rec.message and "42" in rec.message for rec in caplog.records)


def test_allowed_cap_passes_through(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = _build_caps(None, "NET_RAW", chal_id=7)
    assert "NET_RAW" in result
    assert caplog.records == []


def test_mixed_caps_filter_keeps_safe_drops_bad(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = _build_caps(None, "SYS_ADMIN,NET_RAW,SYS_MODULE,NET_ADMIN", chal_id=1)
    assert "NET_RAW" in result
    assert "NET_ADMIN" in result
    assert "SYS_ADMIN" not in result
    assert "SYS_MODULE" not in result
    dropped = [rec.message for rec in caplog.records]
    assert any("SYS_ADMIN" in m for m in dropped)
    assert any("SYS_MODULE" in m for m in dropped)


def test_ssh_default_caps_preserved_even_when_admin_supplies_bad_cap(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = _build_caps("ssh", "SYS_ADMIN", chal_id=99)
    for ssh_cap in _SSH_CAPS:
        assert ssh_cap in result
    assert "SYS_ADMIN" not in result
    assert any("SYS_ADMIN" in rec.message for rec in caplog.records)


def test_empty_cap_add_returns_empty():
    assert _build_caps(None, None) == []
    assert _build_caps(None, "") == []


def test_case_insensitive_allowlist():
    assert "NET_RAW" in _build_caps(None, "net_raw", chal_id=1)
    assert "NET_RAW" in _build_caps(None, "  Net_Raw  ", chal_id=1)


def test_allowlist_contains_expected_ctf_caps():
    assert _ALLOWED_CAPS == frozenset({"NET_ADMIN", "NET_RAW", "SYS_PTRACE", "SYS_NICE"})


def test_filter_admin_caps_direct_helper(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = _filter_admin_caps("NET_RAW,SYS_ADMIN", chal_id="abc")
    assert result == ["NET_RAW"]
    assert any("SYS_ADMIN" in rec.message and "abc" in rec.message for rec in caplog.records)
