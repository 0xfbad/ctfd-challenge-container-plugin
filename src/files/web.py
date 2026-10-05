from __future__ import annotations

import hashlib
import hmac
import json
import os

from flask import (
    Blueprint,
    Flask,
    Response,
    abort,
    current_app,
    g,
    has_request_context,
    jsonify,
    make_response,
    render_template_string,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import HTTPException

from CTFd.models import Challenges, Flags, Solves, Users
from CTFd.utils.decorators import admins_only, authed_only, during_ctf_time_only, require_verified_emails
from CTFd.utils.decorators.visibility import check_challenge_visibility
from CTFd.utils.user import get_current_user

from .. import freshness, utils
from .store import (
    ArtifactUnavailable,
    InvalidRecipe,
    QueueFull,
    RecipeChanged,
    Store,
    StoreBusy,
    StoreError,
    validate_recipe,
)

blueprint = Blueprint("personalized_files", __name__, url_prefix="/plugins/personalized-files")
_FINGERPRINT_DOMAIN = b"ctfd-personalized-files:identity:v1\x00"
_RETRY_SECONDS = 3
_GENERATING_MESSAGE = "Preparing your file. Your download will start automatically when it is ready."
_WAITING_MESSAGE = "Your file is waiting to be prepared. Your download will start automatically when it is ready."

_STATUS_PAGE = """{% extends "base.html" %}
{% block stylesheets %}
{{ super() }}
{% if pending %}<meta http-equiv="refresh" content="{{ retry }}">{% endif %}
{% endblock %}
{% block content %}
<div class="container mt-4">
<div class="alert alert-{{ 'info' if pending else 'danger' }}" role="{{ 'status' if pending else 'alert' }}">
{% if pending %}<span class="spinner-border spinner-border-sm mr-2 me-2" aria-hidden="true"></span>{% endif %}
{{ message }}
</div>
<a class="btn btn-outline-secondary" href="{{ challenges_url }}">Back to challenges</a>
</div>
{% endblock %}"""


def _store() -> Store:
    store = current_app.extensions["personalized_files_store"]
    if store is None:
        store = Store(current_app.config["PERSONALIZED_FILES_ROOT"])
        current_app.extensions["personalized_files_store"] = store
    return store


def _authorize_download(challenge_id: int) -> tuple[Users, bool]:
    user = get_current_user()
    if user is None or user.banned:
        abort(403)
    team_mode = utils.is_team_mode()
    if team_mode is None:
        return abort(503)
    if team_mode and (user.team is None or user.team.banned):
        abort(403)
    challenge = Challenges.query.filter_by(id=challenge_id).first_or_404()
    if user.type == "admin":
        return user, team_mode
    if challenge.state in {"hidden", "locked"}:
        abort(404)
    requirements = (challenge.requirements or {}).get("prerequisites", [])
    if not requirements:
        return user, team_mode

    prerequisites = {
        value for (value,) in Challenges.query.with_entities(Challenges.id).filter(Challenges.id.in_(requirements))
    }
    xid = user.team_id if team_mode else user.id
    solves = {
        value
        for (value,) in Solves.query.with_entities(Solves.challenge_id).filter_by(**utils.owner_filter(xid, team_mode))
    }
    if not prerequisites.issubset(solves):
        abort(403)
    return user, team_mode


def _templates(challenge_id: int) -> list[tuple[str, str]]:
    flags = Flags.query.filter_by(challenge_id=challenge_id, type="freshness").all()
    if any(not isinstance(flag.content, str) for flag in flags):
        raise InvalidRecipe("Personalized files require freshness flags containing %TOKEN%.")
    templates = sorted((flag.content, flag.data or "") for flag in flags)
    if not templates or any("%TOKEN%" not in template for template, _ in templates):
        raise InvalidRecipe("Personalized files require freshness flags containing %TOKEN%.")
    return templates


def _identity(challenge_id: int, user: Users, team_mode: bool) -> tuple[str, str, dict[str, str]]:
    secret = utils.get_setting("freshness_secret")
    if not secret:
        raise StoreError("Freshness tokens are not configured.")
    xid = user.team_id if team_mode else user.id
    if xid is None:
        abort(403)
    length = int(utils.get_setting("freshness_token_length", 6) or 6)
    templates = _templates(challenge_id)
    identity = _owner_identity(challenge_id, xid, team_mode, str(secret), length, templates)
    if utils.is_team_mode() != team_mode:
        raise StoreError("Owner mode changed, reload the challenge.")
    return identity


def _owner_identity(
    challenge_id: int, xid: int, team_mode: bool, secret: str, length: int, templates: list[tuple[str, str]]
) -> tuple[str, str, dict[str, str]]:
    owner = f"{'team' if team_mode else 'user'}:{xid}"
    token = freshness.compute_token(str(secret), challenge_id, xid, length=length)
    flags = [freshness.render_flag(template, token) for template, _ in templates]
    identity = json.dumps(
        {
            "challenge_id": challenge_id,
            "owner": owner,
            "templates": templates,
            "token_length": length,
            "seed_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = hmac.new(str(secret).encode("utf-8"), _FINGERPRINT_DOMAIN + identity, hashlib.sha256).hexdigest()
    environment = {
        "FRESHNESS_TOKEN": token,
        "FRESHNESS_SEED": freshness.compute_seed(str(secret), challenge_id, xid, team_mode=team_mode),
        "FLAGS_JSON": json.dumps(flags),
        "OUTPUT_DIR": "/output",
    }
    if len(flags) == 1:
        environment["FLAG"] = flags[0]
    return owner, fingerprint, environment


def challenge_links(challenge: Challenges) -> list[str]:
    try:
        store = _store()
        recipe = store.get_recipe(challenge.id)
        if recipe is None:
            return []
        validate_recipe(recipe)
    except (StoreError, OSError):
        current_app.logger.warning("Personalized file configuration is unavailable for challenge %s", challenge.id)
        return []
    user = get_current_user() if has_request_context() else None
    if user is not None and user.type != "admin":
        try:
            user, team_mode = _authorize_download(challenge.id)
            observed_order = store._demand_order()
            owner, fingerprint, environment = _identity(challenge.id, user, team_mode)
            store.request(
                challenge.id, owner, fingerprint, environment, expected_recipe=recipe, observed_order=observed_order
            )
        except (StoreError, OSError, HTTPException):
            current_app.logger.debug("File priority deferred for challenge %s", challenge.id)
    return [
        url_for("personalized_files.download", challenge_id=challenge.id, filename=name) for name in recipe["outputs"]
    ]


def _status_response(state: str, status: int, message: str, *, pending: bool = False) -> Response:
    if request.accept_mimetypes.best == "application/json":
        response = jsonify(success=status < 400, state=state, message=message)
        response.status_code = status
    else:
        response = make_response(
            render_template_string(
                _STATUS_PAGE,
                message=message,
                pending=pending,
                retry=_RETRY_SECONDS,
                challenges_url=url_for("challenges.listing"),
            ),
            status,
        )
    if pending or status == 503:
        response.headers["Retry-After"] = str(_RETRY_SECONDS)
    return response


@blueprint.route("/<int:challenge_id>/<filename>", methods=["GET"])
@authed_only
@check_challenge_visibility
@during_ctf_time_only
@require_verified_emails
def download(challenge_id: int, filename: str) -> Response:
    user, team_mode = _authorize_download(challenge_id)
    try:
        store = _store()
        recipe = store.get_recipe(challenge_id)
        if recipe is None:
            abort(404)
        validate_recipe(recipe)
        if filename not in recipe["outputs"]:
            abort(404)
        observed_order = store._demand_order()
        owner, fingerprint, environment = _identity(challenge_id, user, team_mode)
        job = store.request(
            challenge_id, owner, fingerprint, environment, expected_recipe=recipe, observed_order=observed_order
        )
        if job["state"] == "failed":
            return _status_response("failed", 503, "Your file could not be generated. Please contact an organizer.")
        if job["state"] != "ready":
            message = _GENERATING_MESSAGE if job["state"] == "running" else _WAITING_MESSAGE
            return _status_response(job["state"], 202, message, pending=True)
        handle = store.open_file(job, filename)
        if handle is None:
            return _status_response("queued", 202, _WAITING_MESSAGE, pending=True)
        if request.accept_mimetypes.best == "application/json":
            handle.close()
            return jsonify(success=True, state="ready")
        try:
            response = send_file(
                handle,
                as_attachment=True,
                download_name=filename,
                mimetype="application/octet-stream",
                conditional=False,
                etag=False,
                max_age=0,
            )
        except BaseException:
            handle.close()
            raise
        response.headers["X-Accel-Buffering"] = "no"
        response.call_on_close(handle.close)
        g._personalized_files_download = handle
        return response
    except (StoreBusy, QueueFull):
        return _status_response("busy", 503, "File preparation is busy. Retrying shortly.", pending=True)
    except (RecipeChanged, ArtifactUnavailable):
        return _status_response("queued", 202, _WAITING_MESSAGE, pending=True)
    except (StoreError, OSError):
        return _status_response("unavailable", 503, "Your file is unavailable. Please contact an organizer.")


@blueprint.route("/admin/status")
@admins_only
def status() -> Response | tuple[Response, int]:
    user = get_current_user()
    if user is None or user.banned or user.type != "admin":
        abort(403)
    try:
        return jsonify(success=True, data=_store().summary())
    except (StoreError, OSError):
        return jsonify(success=False, error="File generation status is unavailable."), 503


@blueprint.route("/admin/<int:challenge_id>", methods=["GET", "PUT", "DELETE"])
@admins_only
def configure(challenge_id: int) -> Response | tuple[Response, int]:
    user = get_current_user()
    if user is None or user.banned or user.type != "admin":
        abort(403)
    if request.method != "DELETE":
        Challenges.query.filter_by(id=challenge_id).first_or_404()
    try:
        store = _store()
        if request.method == "DELETE":
            store.delete_recipe(challenge_id)
            return jsonify(success=True, recipe=None)
        if request.method == "PUT":
            _templates(challenge_id)
            recipe = store.set_recipe(challenge_id, request.get_json(silent=True))
        else:
            recipe = store.get_recipe(challenge_id)
        return jsonify(success=True, recipe=recipe)
    except InvalidRecipe as error:
        return jsonify(success=False, error=str(error)), 400
    except (StoreError, OSError):
        return jsonify(success=False, error="The personalized file store is unavailable."), 503


def install(app: Flask) -> None:
    root = app.config.get("PERSONALIZED_FILES_ROOT") or os.environ.get("PERSONALIZED_FILES_ROOT")
    if not root:
        root = os.path.join(app.instance_path, "personalized-files")
    upload_root = os.path.realpath(app.config["UPLOAD_FOLDER"])
    root = os.path.realpath(root)
    if os.path.commonpath([root, upload_root]) == upload_root:
        raise RuntimeError("PERSONALIZED_FILES_ROOT must be outside UPLOAD_FOLDER")
    app.config["PERSONALIZED_FILES_ROOT"] = root
    try:
        store = Store(root)
    except (StoreError, OSError) as error:
        app.logger.warning("Personalized file store is unavailable (%s)", type(error).__name__)
        store = None
    app.extensions["personalized_files_store"] = store
    app.extensions["personalized_files_links"] = challenge_links
    app.register_blueprint(blueprint)

    @app.teardown_request
    def close_failed_download(error: BaseException | None) -> None:
        download = g.pop("_personalized_files_download", None)
        if error is not None and download is not None:
            download.close()

    @app.after_request
    def private_downloads(response: Response) -> Response:
        path = request.environ.get("PATH_INFO", "")
        prefix = "/plugins/personalized-files"
        if request.blueprint == blueprint.name or path == prefix or path.startswith(f"{prefix}/"):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.vary.update(("Cookie", "Authorization"))
        return response
