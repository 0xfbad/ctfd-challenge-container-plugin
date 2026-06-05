import sys
from unittest.mock import patch, MagicMock

_helpers = sys.modules["_cc_plugin.views.helpers"]
_create_container_inner = _helpers._create_container_inner

_MOD = "_cc_plugin.views.helpers"


def _build_challenge():
    challenge = MagicMock()
    challenge.id = 1
    challenge.image = "test:latest"
    challenge.port = 80
    challenge.command = ""
    challenge.volumes = ""
    challenge.max_memory_mb = None
    challenge.max_cpu = None
    challenge.docker_context = None
    challenge.expiration_seconds = 600
    challenge.ctype = "tcp"
    challenge.ssh_username = None
    challenge.ssh_password = None
    challenge.services_json = None
    challenge.network_json = None
    challenge.cap_add = None
    return challenge


def _patches(quota_count):
    # captures the filter_by kwargs the quota query was called with,
    # both dedupe and quota go through filter_by so we inspect call_args_list
    mock_app = MagicMock()
    mock_app.container_manager = MagicMock()

    return patch.multiple(
        _MOD,
        current_app=mock_app,
        ContainerChallengeModel=MagicMock(),
        ContainerInfoModel=MagicMock(),
        get_setting=MagicMock(
            side_effect=lambda k, *a: {"max_containers_per_user": 2, "freshness_secret": ""}.get(
                k, a[0] if a else None
            )
        ),
        db=MagicMock(),
    )


def test_quota_in_team_mode_filters_by_team_id():
    # team_id 7, calling member user_id 12, mocked count above the limit
    with _patches(quota_count=2):
        ccm = sys.modules[_MOD].ContainerChallengeModel
        cim = sys.modules[_MOD].ContainerInfoModel
        ccm.query.filter_by.return_value.first.return_value = _build_challenge()
        cim.query.filter_by.return_value.count.return_value = 2

        result = _create_container_inner(1, 7, 12, True)

        # quota hit returns 409, confirms the count() path ran
        assert result[1] == 409

        # first filter_by is on the challenge lookup, second on quota, third on dedupe
        # quota call must scope by team_id=7, NOT user_id=12
        quota_call = cim.query.filter_by.call_args_list[0]
        assert quota_call.kwargs == {"team_id": 7}
        assert "user_id" not in quota_call.kwargs


def test_quota_in_user_mode_filters_by_user_id():
    with _patches(quota_count=2):
        ccm = sys.modules[_MOD].ContainerChallengeModel
        cim = sys.modules[_MOD].ContainerInfoModel
        ccm.query.filter_by.return_value.first.return_value = _build_challenge()
        cim.query.filter_by.return_value.count.return_value = 2

        result = _create_container_inner(1, 12, 12, False)

        assert result[1] == 409

        quota_call = cim.query.filter_by.call_args_list[0]
        assert quota_call.kwargs == {"user_id": 12}
        assert "team_id" not in quota_call.kwargs
