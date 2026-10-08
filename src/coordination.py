from __future__ import annotations

import calendar
import random
import secrets
import string
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from CTFd.models import db

from .models import (
    ContainerHistoryModel,
    ContainerInfoModel,
    ContainerInstanceModel,
    DockerContextModel,
)

RESOURCE_OWNING_STATES = ("provisioning", "running", "cleanup_pending")


class CoordinationError(Exception):
    pass


class InstanceQuotaExceeded(CoordinationError):
    pass


class CreateCapacityUnavailable(CoordinationError):
    pass


class ContextUnavailable(CoordinationError):
    pass


@dataclass(frozen=True)
class InstanceReservation:
    instance_id: str
    provision_token: str
    context_id: int
    context_name: str
    quota_slot: int
    create_slot: int | None
    placement_units: int
    state: str
    created: bool
    ssh_password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PhysicalMember:
    container_id: str
    port: int
    is_entry: bool
    service_name: str


@dataclass(frozen=True)
class LifecycleUpdate:
    expires: int
    renewals_used: int
    solved_at: float | None


@dataclass(frozen=True)
class FinalizedInstance:
    container_id: str
    port: int
    expires: int
    renewals_used: int
    user_id: int | None
    team_id: int | None
    docker_context: str
    ssh_password: str | None = field(default=None, repr=False)


def owner_key(xid: int, is_team: bool) -> str:
    if xid <= 0:
        raise ValueError("owner id must be positive")

    return f"team:{xid}" if is_team else f"user:{xid}"


def _new_session():
    return sessionmaker(bind=db.engine, expire_on_commit=False)()  # race rollbacks must not invalidate request state


def _reservation(row: ContainerInstanceModel, *, created: bool) -> InstanceReservation:
    context = row.docker_context
    if context is None or row.docker_context_id is None:
        raise CoordinationError("reserved instance has no docker context")

    return InstanceReservation(
        instance_id=row.id,
        provision_token=row.provision_token,
        context_id=row.docker_context_id,
        context_name=context.context_name,
        quota_slot=row.quota_slot,
        create_slot=row.create_slot,
        placement_units=row.placement_units,
        state=row.state,
        created=created,
        ssh_password=row.ssh_password,
    )


@dataclass(frozen=True)
class _ReserveRequest:
    identity: str
    challenge_id: int
    xid: int
    is_team: bool
    submitter_user_id: int
    max_instances: int
    max_concurrent_creates: int
    placement_units: int
    expires: int
    provision_timeout_seconds: int
    preferred_context_name: str | None
    eligible_context_names: set[str] | None
    instance_id: str
    provision_token: str
    generate_ssh_password: bool


class InstanceCoordinator:
    SQLITE_BUSY_RETRIES = 5

    @staticmethod
    def get_lifecycle(instance_id: str) -> LifecycleUpdate | None:
        session = _new_session()
        try:
            row = session.query(ContainerInstanceModel).filter_by(id=instance_id).first()
            if row is None:
                return None

            return LifecycleUpdate(int(row.expires), int(row.renewals_used), row.solved_at)
        finally:
            session.close()

    @staticmethod
    def placement_counts(session=None) -> dict[int, int]:
        owns_session = session is None
        session = session or _new_session()
        try:
            rows = (
                session.query(
                    ContainerInstanceModel.docker_context_id,
                    func.coalesce(func.sum(ContainerInstanceModel.placement_units), 0),
                )
                .filter(
                    ContainerInstanceModel.docker_context_id.isnot(None),
                    ContainerInstanceModel.state.in_(RESOURCE_OWNING_STATES),  # cleanup_pending still owns placement
                )
                .group_by(ContainerInstanceModel.docker_context_id)
                .all()
            )
            return {int(context_id): int(units) for context_id, units in rows}
        finally:
            if owns_session:
                session.close()

    def reserve_instance(
        self,
        *,
        challenge_id: int,
        xid: int,
        is_team: bool,
        submitter_user_id: int,
        max_instances: int,
        max_concurrent_creates: int,
        placement_units: int,
        expires: int,
        preferred_context_name: str | None = None,
        eligible_context_names: set[str] | None = None,
        provision_timeout_seconds: int = 120,
        generate_ssh_password: bool = False,
    ) -> InstanceReservation:
        self._validate_reserve_args(
            challenge_id=challenge_id,
            submitter_user_id=submitter_user_id,
            max_instances=max_instances,
            max_concurrent_creates=max_concurrent_creates,
            placement_units=placement_units,
            provision_timeout_seconds=provision_timeout_seconds,
            preferred_context_name=preferred_context_name,
            eligible_context_names=eligible_context_names,
        )

        request = _ReserveRequest(
            identity=owner_key(xid, is_team),
            challenge_id=challenge_id,
            xid=xid,
            is_team=is_team,
            submitter_user_id=submitter_user_id,
            max_instances=max_instances,
            max_concurrent_creates=max_concurrent_creates,
            placement_units=placement_units,
            expires=expires,
            provision_timeout_seconds=provision_timeout_seconds,
            preferred_context_name=preferred_context_name,
            eligible_context_names=eligible_context_names,
            instance_id=uuid.uuid4().hex,
            provision_token=uuid.uuid4().hex,
            generate_ssh_password=generate_ssh_password,
        )
        busy_attempt = 0

        max_attempts = max_instances * max_concurrent_creates * 8 + 8  # retries cover quota and create slot collisions
        for _ in range(max_attempts):
            session = _new_session()
            try:
                reservation = self._attempt_reserve(session, request)
                if reservation is not None:
                    return reservation
            except IntegrityError:
                session.rollback()  # the next pass distinguishes a competing owner from quota exhaustion
            except OperationalError as error:
                session.rollback()
                if (
                    "locked" not in str(error).lower() or busy_attempt >= self.SQLITE_BUSY_RETRIES
                ):  # sqlite writer conflicts need bounded retries
                    raise
                busy_attempt += 1
                time.sleep(random.uniform(0.005, 0.025) * busy_attempt)
            finally:
                session.close()

        session = _new_session()
        try:
            existing = self._existing_reservation(
                session, request.identity, request.challenge_id
            )  # a late owner insert may have won
            if existing is not None:
                return existing
        finally:
            session.close()

        raise CreateCapacityUnavailable("could not reserve an instance after concurrent updates")

    def _attempt_reserve(self, session, request: _ReserveRequest) -> InstanceReservation | None:
        existing = self._existing_reservation(session, request.identity, request.challenge_id)
        if existing is not None:
            return existing

        quota_slot = self._pick_quota_slot(session, request.identity, request.max_instances)
        contexts = self._ranked_contexts(session, request.preferred_context_name, request.eligible_context_names)
        context, create_slot = self._pick_create_slot(session, contexts, request.max_concurrent_creates)

        if not self._bump_placement_version(session, context):  # placement and admission commit together against drains
            session.rollback()
            return None

        instance = self._provisioning_row(
            request, context_id=context.id, quota_slot=quota_slot, create_slot=create_slot
        )
        session.add(instance)
        session.commit()

        return _reservation(instance, created=True)  # expire_on_commit must stay false for this committed snapshot

    @staticmethod
    def _validate_reserve_args(
        *,
        challenge_id: int,
        submitter_user_id: int,
        max_instances: int,
        max_concurrent_creates: int,
        placement_units: int,
        provision_timeout_seconds: int,
        preferred_context_name: str | None,
        eligible_context_names: set[str] | None,
    ) -> None:
        if challenge_id <= 0:
            raise ValueError("challenge id must be positive")
        if submitter_user_id <= 0:
            raise ValueError("submitter user id must be positive")
        if max_instances <= 0:
            raise ValueError("max_instances must be positive")
        if max_concurrent_creates <= 0:
            raise ValueError("max_concurrent_creates must be positive")
        if placement_units <= 0:
            raise ValueError("placement_units must be positive")
        if provision_timeout_seconds <= 0:
            raise ValueError("provision timeout must be positive")
        if eligible_context_names is not None and not eligible_context_names:
            raise ContextUnavailable("no docker context satisfies the challenge prerequisites")
        if (
            preferred_context_name
            and eligible_context_names is not None
            and preferred_context_name not in eligible_context_names
        ):
            raise ContextUnavailable(f"docker context '{preferred_context_name}' does not satisfy prerequisites")

    @staticmethod
    def _existing_reservation(session, identity: str, challenge_id: int) -> InstanceReservation | None:
        existing = (
            session.query(ContainerInstanceModel).filter_by(owner_key=identity, challenge_id=challenge_id).first()
        )
        if existing is None:
            return None

        return _reservation(existing, created=False)

    @staticmethod
    def _pick_quota_slot(session, identity: str, max_instances: int) -> int:
        used_quota = {
            int(slot)
            for (slot,) in session.query(ContainerInstanceModel.quota_slot).filter_by(owner_key=identity).all()
        }
        quota_slot = next((slot for slot in range(max_instances) if slot not in used_quota), None)
        if quota_slot is None:
            raise InstanceQuotaExceeded("owner instance quota is full")

        return quota_slot

    def _ranked_contexts(
        self, session, preferred_context_name: str | None, eligible_context_names: set[str] | None
    ) -> list[DockerContextModel]:
        contexts_query = session.query(DockerContextModel).filter(
            DockerContextModel.state == "active",
            DockerContextModel.health_state == "healthy",
        )
        if preferred_context_name:
            contexts_query = contexts_query.filter(DockerContextModel.context_name == preferred_context_name)
        if eligible_context_names is not None:
            contexts_query = contexts_query.filter(DockerContextModel.context_name.in_(sorted(eligible_context_names)))
        contexts = contexts_query.all()
        if not contexts:
            if preferred_context_name:
                raise ContextUnavailable(f"docker context '{preferred_context_name}' is not available")
            raise ContextUnavailable("no healthy active docker context is available")

        counts = self.placement_counts(session)
        contexts.sort(
            key=lambda context: (
                -(int(context.weight) / (counts.get(context.id, 0) + 1)),
                context.context_name,
            )
        )
        return contexts

    @staticmethod
    def _pick_create_slot(
        session, contexts: list[DockerContextModel], max_concurrent_creates: int
    ) -> tuple[DockerContextModel, int]:
        for context in contexts:
            used_create_slots = {
                int(slot)
                for (slot,) in session.query(ContainerInstanceModel.create_slot)
                .filter(
                    ContainerInstanceModel.docker_context_id == context.id,
                    ContainerInstanceModel.create_slot.isnot(None),
                )
                .all()
            }
            free_slots = [slot for slot in range(max_concurrent_creates) if slot not in used_create_slots]
            if free_slots:
                return context, random.choice(free_slots)

        raise CreateCapacityUnavailable("all docker context create slots are busy")

    @staticmethod
    def _bump_placement_version(session, context: DockerContextModel) -> bool:
        updated = (
            session.query(DockerContextModel)
            .filter(
                DockerContextModel.id == context.id,
                DockerContextModel.state == "active",
                DockerContextModel.health_state == "healthy",
                DockerContextModel.hostname == context.hostname,
            )
            .update(
                {
                    DockerContextModel.placement_version: DockerContextModel.placement_version + 1
                },  # this update locks the row against drains
                synchronize_session=False,
            )
        )
        return updated == 1

    @staticmethod
    def _provisioning_row(
        request: _ReserveRequest, *, context_id: int, quota_slot: int, create_slot: int
    ) -> ContainerInstanceModel:
        now = time.time()
        return ContainerInstanceModel(
            id=request.instance_id,
            ssh_password="".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
            if request.generate_ssh_password
            else None,
            owner_key=request.identity,
            user_id=request.submitter_user_id,
            team_id=request.xid if request.is_team else None,
            challenge_id=request.challenge_id,
            quota_slot=quota_slot,
            state="provisioning",
            state_version=0,
            docker_context_id=context_id,
            create_slot=create_slot,
            placement_units=request.placement_units,
            provision_token=request.provision_token,
            provision_deadline=now + request.provision_timeout_seconds,
            created_at=now,
            updated_at=now,
            expires=request.expires,
            renewals_used=0,
        )

    @staticmethod
    def mark_running(
        instance_id: str,
        provision_token: str,
        entry_container_id: str,
        *,
        stack_id: str | None = None,
        physical_members: tuple[PhysicalMember, ...],
    ) -> FinalizedInstance | None:
        entries = [member for member in physical_members if member.is_entry]
        if len(entries) != 1 or entries[0].container_id != entry_container_id:
            raise ValueError("physical members must contain exactly the declared entry container")

        service_names = [member.service_name for member in physical_members]
        if len(service_names) != len(set(service_names)):
            raise ValueError("physical member service names must be unique")

        session = _new_session()
        try:
            now = time.time()
            instance = (
                session.query(ContainerInstanceModel)
                .filter(
                    ContainerInstanceModel.id == instance_id,
                    ContainerInstanceModel.provision_token == provision_token,
                    ContainerInstanceModel.state == "provisioning",
                )
                .first()
            )
            if instance is None:
                session.rollback()
                return None

            if len(physical_members) != instance.placement_units:
                raise ValueError("physical member count does not match reserved placement units")

            context_name = instance.docker_context.context_name
            created_timestamp = int(instance.created_at)
            for member in physical_members:
                session.add(
                    ContainerInfoModel(
                        container_id=member.container_id,
                        instance_id=instance.id,
                        challenge_id=instance.challenge_id,
                        team_id=instance.team_id,
                        user_id=instance.user_id,
                        port=member.port,
                        timestamp=created_timestamp,
                        expires=instance.expires,
                        renewals_used=instance.renewals_used,
                        docker_context=context_name,
                        stack_id=stack_id,
                        is_entry=member.is_entry,
                    )
                )
                session.add(
                    ContainerHistoryModel(
                        instance_id=instance.id,
                        container_id=member.container_id,
                        challenge_id=instance.challenge_id,
                        user_id=instance.user_id,
                        team_id=instance.team_id,
                        docker_context=context_name,
                        stack_id=stack_id,
                        is_entry=member.is_entry,
                        service_name=member.service_name,
                        created_at=now,
                    )
                )

            updated = (
                session.query(ContainerInstanceModel)
                .filter(
                    ContainerInstanceModel.id == instance_id,
                    ContainerInstanceModel.provision_token == provision_token,
                    ContainerInstanceModel.state == "provisioning",
                )
                .update(
                    {
                        ContainerInstanceModel.state: "running",
                        ContainerInstanceModel.state_version: ContainerInstanceModel.state_version + 1,
                        ContainerInstanceModel.create_slot: None,
                        ContainerInstanceModel.entry_container_id: entry_container_id,
                        ContainerInstanceModel.stack_id: stack_id,
                        ContainerInstanceModel.provision_deadline: None,
                        ContainerInstanceModel.updated_at: now,
                        ContainerInstanceModel.last_error: None,
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                session.rollback()
                return None

            entry = entries[0]
            result = FinalizedInstance(
                container_id=entry_container_id,
                port=entry.port,
                expires=int(instance.expires),
                renewals_used=int(instance.renewals_used),
                user_id=instance.user_id,
                team_id=instance.team_id,
                docker_context=instance.docker_context.context_name,
                ssh_password=instance.ssh_password,
            )
            session.commit()
            return result
        finally:
            session.close()

    @staticmethod
    def mark_cleanup_pending(instance_id: str, provision_token: str, error: str) -> bool:
        session = _new_session()
        try:
            updated = (
                session.query(ContainerInstanceModel)
                .filter(
                    ContainerInstanceModel.id == instance_id,
                    ContainerInstanceModel.provision_token == provision_token,
                    ContainerInstanceModel.state == "provisioning",
                )
                .update(
                    {
                        ContainerInstanceModel.state: "cleanup_pending",  # docker absence must precede release of create admission
                        ContainerInstanceModel.state_version: ContainerInstanceModel.state_version + 1,
                        ContainerInstanceModel.updated_at: time.time(),
                        ContainerInstanceModel.last_error: str(error)[:512],
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            return updated == 1
        finally:
            session.close()

    @staticmethod
    def delete_after_confirmed_cleanup(
        instance_id: str,
        *,
        provision_token: str | None = None,
        operation_token: str | None = None,
        reason: str | None = None,
        stopped_at: float | None = None,
    ) -> bool:
        if provision_token is None and operation_token is None:
            raise ValueError("a provision or operation token is required")

        session = _new_session()
        try:
            query = session.query(ContainerInstanceModel).filter(ContainerInstanceModel.id == instance_id)
            if provision_token is not None:
                query = query.filter(ContainerInstanceModel.provision_token == provision_token)
            if operation_token is not None:
                query = query.filter(ContainerInstanceModel.operation_token == operation_token)
            if query.first() is None:
                session.rollback()
                return False

            if reason is not None:
                session.query(ContainerHistoryModel).filter_by(instance_id=instance_id).update(
                    {
                        ContainerHistoryModel.stopped_at: stopped_at or time.time(),
                        ContainerHistoryModel.reason: case(
                            [(ContainerHistoryModel.reason.is_(None), reason)],
                            else_=ContainerHistoryModel.reason,
                        ),
                    },
                    synchronize_session=False,
                )
            session.query(ContainerInfoModel).filter_by(instance_id=instance_id).delete(
                synchronize_session=False
            )  # the foreign key blocks parent deletion
            deleted = query.delete(synchronize_session=False)
            session.commit()
            return deleted == 1
        finally:
            session.close()

    @staticmethod
    def release_operation(instance_id: str, operation_token: str, error: str | None = None) -> bool:
        session = _new_session()
        try:
            updated = (
                session.query(ContainerInstanceModel)
                .filter(
                    ContainerInstanceModel.id == instance_id,
                    ContainerInstanceModel.operation_token == operation_token,
                    ContainerInstanceModel.state == "cleanup_pending",
                )
                .update(
                    {
                        ContainerInstanceModel.operation_token: None,
                        ContainerInstanceModel.updated_at: time.time(),
                        ContainerInstanceModel.last_error: str(error)[:512] if error else None,
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            return updated == 1
        finally:
            session.close()

    @staticmethod
    def claim_operation(
        instance_id: str,
        expected_states: tuple[str, ...],
        target_state: str,
        *,
        stale_after_seconds: int = 120,
        expected_state_version: int | None = None,
        maintenance_cutoff: int | None = None,
        reconcile_safety_age_seconds: int | None = None,
    ) -> str | None:
        if not expected_states:
            raise ValueError("at least one expected state is required")

        if target_state != "cleanup_pending":
            raise ValueError("invalid operation target state")

        if stale_after_seconds <= 0:
            raise ValueError("operation lease must be positive")

        conditions = [
            ContainerInstanceModel.id == instance_id,
            ContainerInstanceModel.state.in_(expected_states),
        ]
        if expected_state_version is not None:
            conditions.append(ContainerInstanceModel.state_version == expected_state_version)

        if maintenance_cutoff is not None:
            if (
                expected_state_version is None
                or reconcile_safety_age_seconds is None
                or reconcile_safety_age_seconds <= 0
            ):
                raise ValueError("maintenance claims require a state version and positive safety age")

            stale_before = maintenance_cutoff - reconcile_safety_age_seconds
            conditions.append(
                ((ContainerInstanceModel.state == "running") & (ContainerInstanceModel.expires < maintenance_cutoff))
                | (
                    (ContainerInstanceModel.state == "cleanup_pending")
                    & (ContainerInstanceModel.updated_at < stale_before)
                )
                | (
                    (ContainerInstanceModel.state == "provisioning")
                    & ContainerInstanceModel.provision_deadline.isnot(None)
                    & (ContainerInstanceModel.provision_deadline < stale_before)
                )
            )

        token = uuid.uuid4().hex
        session = _new_session()
        try:
            now = time.time()
            updated = (
                session.query(ContainerInstanceModel)
                .filter(
                    *conditions,
                    (
                        ContainerInstanceModel.operation_token.is_(None)
                        | (ContainerInstanceModel.updated_at < now - stale_after_seconds)
                    ),
                )
                .update(
                    {
                        ContainerInstanceModel.state: target_state,
                        ContainerInstanceModel.state_version: ContainerInstanceModel.state_version + 1,
                        ContainerInstanceModel.operation_token: token,
                        ContainerInstanceModel.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            return token if updated == 1 else None
        finally:
            session.close()

    @staticmethod
    def shorten_after_solve(
        instance_id: str, target_expires: int, solved_at: float | None = None
    ) -> LifecycleUpdate | None:
        solved_at = solved_at if solved_at is not None else time.time()
        session = _new_session()
        try:
            shortened = case(
                [
                    (
                        ContainerInstanceModel.expires > target_expires,
                        target_expires,
                    )
                ],
                else_=ContainerInstanceModel.expires,
            )
            updated = (
                session.query(ContainerInstanceModel)
                .filter(
                    ContainerInstanceModel.id == instance_id,
                    ContainerInstanceModel.state == "running",
                    ContainerInstanceModel.solved_at.is_(None),
                )
                .update(
                    {
                        ContainerInstanceModel.expires: shortened,
                        ContainerInstanceModel.solved_at: solved_at,
                        ContainerInstanceModel.state_version: ContainerInstanceModel.state_version + 1,
                        ContainerInstanceModel.updated_at: solved_at,
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                session.rollback()
                return None

            row = session.query(ContainerInstanceModel).filter_by(id=instance_id).one()
            session.query(ContainerInfoModel).filter_by(instance_id=instance_id).update(
                {ContainerInfoModel.expires: row.expires}, synchronize_session=False
            )
            session.query(ContainerHistoryModel).filter_by(instance_id=instance_id).update(
                {ContainerHistoryModel.reason: "solved"}, synchronize_session=False
            )
            result = LifecycleUpdate(int(row.expires), int(row.renewals_used), row.solved_at)
            session.commit()
            return result
        finally:
            session.close()

    @staticmethod
    def renew(instance_id: str, *, now: int, new_expires: int, max_renewals: int) -> LifecycleUpdate | None:
        session = _new_session()
        try:
            updated = (
                session.query(ContainerInstanceModel)
                .filter(
                    ContainerInstanceModel.id == instance_id,
                    ContainerInstanceModel.state == "running",
                    ContainerInstanceModel.solved_at.is_(None),
                    ContainerInstanceModel.expires > now,
                    ContainerInstanceModel.renewals_used < max_renewals,
                )
                .update(
                    {
                        ContainerInstanceModel.expires: new_expires,
                        ContainerInstanceModel.renewals_used: ContainerInstanceModel.renewals_used + 1,
                        ContainerInstanceModel.state_version: ContainerInstanceModel.state_version + 1,
                        ContainerInstanceModel.updated_at: time.time(),
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                session.rollback()
                return None

            row = session.query(ContainerInstanceModel).filter_by(id=instance_id).one()
            session.query(ContainerInfoModel).filter_by(instance_id=instance_id).update(
                {
                    ContainerInfoModel.expires: row.expires,
                    ContainerInfoModel.renewals_used: row.renewals_used,
                },
                synchronize_session=False,
            )
            result = LifecycleUpdate(int(row.expires), int(row.renewals_used), row.solved_at)
            session.commit()
            return result
        finally:
            session.close()

    @staticmethod
    def extend_by_admin(instance_id: str, *, new_expires: int) -> LifecycleUpdate | None:
        session = _new_session()
        try:
            updated = (
                session.query(ContainerInstanceModel)
                .filter(
                    ContainerInstanceModel.id == instance_id,
                    ContainerInstanceModel.state == "running",
                    ContainerInstanceModel.expires < new_expires,
                )
                .update(
                    {
                        ContainerInstanceModel.expires: new_expires,
                        ContainerInstanceModel.state_version: ContainerInstanceModel.state_version + 1,
                        ContainerInstanceModel.updated_at: time.time(),
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                session.rollback()
                return None

            row = session.query(ContainerInstanceModel).filter_by(id=instance_id).one()
            session.query(ContainerInfoModel).filter_by(instance_id=instance_id).update(
                {ContainerInfoModel.expires: row.expires}, synchronize_session=False
            )
            result = LifecycleUpdate(int(row.expires), int(row.renewals_used), row.solved_at)
            session.commit()
            return result
        finally:
            session.close()

    @staticmethod
    def reconcile_solved_instances(shorten_seconds: int) -> int:
        if shorten_seconds <= 0:
            return 0

        from CTFd.models import Solves

        candidates = ContainerInstanceModel.query.filter_by(state="running", solved_at=None).all()
        reconciled = 0
        for instance in candidates:
            if instance.team_id is None and instance.user_id is None:
                continue

            solve_query = Solves.query.filter_by(challenge_id=instance.challenge_id)
            if instance.team_id is not None:
                solve_query = solve_query.filter_by(team_id=instance.team_id)
            else:
                solve_query = solve_query.filter_by(user_id=instance.user_id)

            solve = solve_query.order_by(Solves.date.asc()).first()
            if solve is None or solve.date is None:
                continue

            solved_at = calendar.timegm(solve.date.utctimetuple()) + solve.date.microsecond / 1_000_000
            target_expires = int(solved_at) + shorten_seconds
            if InstanceCoordinator.shorten_after_solve(instance.id, target_expires, solved_at=solved_at):
                reconciled += 1

        return reconciled
