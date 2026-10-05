from __future__ import annotations

from bisect import bisect_left

from flask import Flask

from CTFd.models import Challenges, Teams, Users, db
from CTFd.utils import get_config
from CTFd.utils.dates import ctf_ended

from .. import utils
from .store import StoreError, validate_recipe
from .web import _owner_identity, _store, _templates


def reconcile(app: Flask, *, batch_size: int = 64) -> None:
    with app.app_context():
        try:
            if "personalized_files_store" not in app.extensions:
                return
            if get_config("challenge_visibility") == "admins" or ctf_ended():
                return
            team_mode = utils.is_team_mode()
            secret = utils.get_setting("freshness_secret")
            if team_mode is None or not secret:
                return
            length = int(utils.get_setting("freshness_token_length", 6) or 6)
            store = _store()
            with store.lock("preparation"):
                _prepare_batch(store, team_mode, str(secret), length, batch_size)
        except (OSError, StoreError) as error:
            app.logger.debug("File preparation deferred (%s)", type(error).__name__)
        finally:
            db.session.remove()


def _prepare_batch(store, team_mode, secret, length, batch_size):
    observed_order = store.foreground_order()
    recipe_ids = [
        int(path.stem)
        for path in (store.root / "recipes").glob("*.json")
        if path.stem.isdecimal() and int(path.stem) > 0
    ]
    if not recipe_ids:
        return
    existing = {
        value for (value,) in Challenges.query.with_entities(Challenges.id).filter(Challenges.id.in_(recipe_ids))
    }
    missing = sorted(set(recipe_ids) - existing)
    if missing and batch_size > 0:
        store.delete_recipe(missing[0])
    challenges = [
        value
        for (value,) in Challenges.query.with_entities(Challenges.id)
        .filter(Challenges.id.in_(recipe_ids), Challenges.state == "visible")
        .order_by(Challenges.id)
    ]
    if not challenges:
        return

    column = Users.team_id if team_mode else Users.id
    owners = Users.query.with_entities(column).filter(Users.type == "user", Users.banned.is_(False))
    if get_config("verify_emails"):
        owners = owners.filter(Users.verified.is_(True))
    if team_mode:
        owners = owners.join(Teams, Teams.id == Users.team_id).filter(Teams.banned.is_(False))
    owners = owners.distinct()

    path = store.root / ".preparation.json"
    try:
        cursor = store._read(path) or [0, 0]
    except StoreError:
        cursor = [0, 0]
    if not isinstance(cursor, list) or len(cursor) != 2 or any(type(value) is not int or value < 0 for value in cursor):
        cursor = [0, 0]
    index = bisect_left(challenges, cursor[0])
    if index == len(challenges):
        index = 0
    owner_id = cursor[1] if challenges[index] == cursor[0] else 0
    start = index
    examined = 0
    while examined < batch_size:
        challenge_id = challenges[index]
        try:
            recipe = validate_recipe(store.get_recipe(challenge_id))
            templates = _templates(challenge_id)
        except StoreError:
            templates = None
        candidates = (
            owners.with_session(db.session())
            .filter(column > owner_id)
            .order_by(column)
            .limit(batch_size - examined)
            .all()
        )
        for (owner_id,) in candidates:
            cursor = [challenge_id, owner_id]
            examined += 1
            if templates is None:
                continue
            owner, fingerprint, environment = _owner_identity(
                challenge_id, owner_id, team_mode, secret, length, templates
            )
            db.session.remove()  # fresh transactions observe author edits during a preparation batch
            try:
                current = (
                    str(utils.get_setting("freshness_secret")) == secret
                    and int(utils.get_setting("freshness_token_length", 6) or 6) == length
                    and _templates(challenge_id) == templates
                    and utils.is_team_mode() == team_mode
                )
            except StoreError:
                current = False
            if not current:
                store._write(path, cursor)
                return
            try:
                store.request(
                    challenge_id,
                    owner,
                    fingerprint,
                    environment,
                    expected_recipe=recipe,
                    background=True,
                    observed_order=observed_order,
                )
            except StoreError:
                continue
        if examined == batch_size:
            break
        index = (index + 1) % len(challenges)
        owner_id = 0
        cursor = [challenges[index], 0]
        if index == start:
            break
    store._write(path, cursor)
