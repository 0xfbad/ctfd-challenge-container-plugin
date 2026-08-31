"""Database coordination for logical challenge-container instances.

All methods in this module use short, independent SQLAlchemy sessions. Docker
and SSH calls must happen *after* a reservation transaction commits and before
one of the CAS completion methods is called. This keeps slow external I/O out
of database transactions while unique constraints remain the cross-process
correctness boundary.
"""

from __future__ import annotations

import calendar
import random
import time
import uuid
from dataclasses import dataclass

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
    """Base error for an expected coordination outcome."""


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


def owner_key(xid: int, is_team: bool) -> str:
    """Return the non-null server identity used by uniqueness constraints."""

    if xid <= 0:
        raise ValueError("owner id must be positive")
    return f"team:{xid}" if is_team else f"user:{xid}"


def _new_session():
    # Do not use Flask-SQLAlchemy's request-scoped session here. A caller may
    # already have loaded ORM objects, and rolling that session back after a
    # uniqueness race would invalidate unrelated request state.
    return sessionmaker(bind=db.engine, expire_on_commit=False)()


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
    )


class InstanceCoordinator:
    """Portable DB-backed quota, placement, and lifecycle coordination."""

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
        """Return conservative physical placement units by context ID.

        ``placement_units`` is the validated physical member count for a
        logical instance. It deliberately remains charged through stopping and
        cleanup-pending states, until absence of Docker resources is confirmed.
        """

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
                    ContainerInstanceModel.state.in_(RESOURCE_OWNING_STATES),
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
    ) -> InstanceReservation:
        """Atomically reserve owner quota, a host, and one host create slot.

        If another worker already owns the same owner/challenge reservation,
        that durable row is returned with ``created=False``. No transaction is
        retained after this method returns.
        """

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

        identity = owner_key(xid, is_team)
        instance_id = uuid.uuid4().hex
        provision_token = uuid.uuid4().hex
        busy_attempt = 0

        # Every uniqueness collision is retried from a fresh database snapshot.
        # The upper bound accommodates simultaneous quota and create-slot races.
        max_attempts = max_instances * max_concurrent_creates * 8 + 8
        for _ in range(max_attempts):
            session = _new_session()
            try:
                existing = (
                    session.query(ContainerInstanceModel)
                    .filter_by(owner_key=identity, challenge_id=challenge_id)
                    .first()
                )
                if existing is not None:
                    return _reservation(existing, created=False)

                used_quota = {
                    int(slot)
                    for (slot,) in session.query(ContainerInstanceModel.quota_slot).filter_by(owner_key=identity).all()
                }
                quota_slot = next((slot for slot in range(max_instances) if slot not in used_quota), None)
                if quota_slot is None:
                    raise InstanceQuotaExceeded("owner instance quota is full")

                contexts_query = session.query(DockerContextModel).filter(
                    DockerContextModel.state == "active",
                    DockerContextModel.health_state == "healthy",
                )
                if preferred_context_name:
                    contexts_query = contexts_query.filter(DockerContextModel.context_name == preferred_context_name)
                if eligible_context_names is not None:
                    contexts_query = contexts_query.filter(
                        DockerContextModel.context_name.in_(sorted(eligible_context_names))
                    )
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

                choice: tuple[DockerContextModel, int] | None = None
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
                    create_slot = next(
                        (slot for slot in range(max_concurrent_creates) if slot not in used_create_slots),
                        None,
                    )
                    if create_slot is not None:
                        choice = context, create_slot
                        break

                if choice is None:
                    raise CreateCapacityUnavailable("all docker context create slots are busy")

                context, create_slot = choice
                observed_placement_version = int(context.placement_version)
                # Linearizes placement against an admin drain/disable. The
                # version bump and instance insert commit or roll back together.
                updated = (
                    session.query(DockerContextModel)
                    .filter(
                        DockerContextModel.id == context.id,
                        DockerContextModel.placement_version == observed_placement_version,
                        DockerContextModel.state == "active",
                        DockerContextModel.health_state == "healthy",
                    )
                    .update(
                        {DockerContextModel.placement_version: DockerContextModel.placement_version + 1},
                        synchronize_session=False,
                    )
                )
                if updated != 1:
                    session.rollback()
                    continue

                now = time.time()
                instance = ContainerInstanceModel(
                    id=instance_id,
                    owner_key=identity,
                    user_id=submitter_user_id,
                    team_id=xid if is_team else None,
                    challenge_id=challenge_id,
                    quota_slot=quota_slot,
                    state="provisioning",
                    state_version=0,
                    docker_context_id=context.id,
                    create_slot=create_slot,
                    placement_units=placement_units,
                    provision_token=provision_token,
                    provision_deadline=now + provision_timeout_seconds,
                    created_at=now,
                    updated_at=now,
                    expires=expires,
                    renewals_used=0,
                )
                session.add(instance)
                session.commit()
                # The context object was loaded in this independent session and
                # remains usable because expire_on_commit=False.
                return _reservation(instance, created=True)
            except IntegrityError:
                session.rollback()
                # A competing owner/quota/create-slot insert won. Retry from a
                # clean snapshot; the next pass distinguishes dedupe from quota.
                continue
            except OperationalError as error:
                session.rollback()
                # SQLite has one writer and may surface SQLITE_BUSY rather than
                # queueing. Other operational failures are not safe to mask.
                if "locked" not in str(error).lower() or busy_attempt >= self.SQLITE_BUSY_RETRIES:
                    raise
                busy_attempt += 1
                time.sleep(random.uniform(0.005, 0.025) * busy_attempt)
            finally:
                session.close()

        # Re-read once to turn a final owner/challenge race into idempotent success.
        session = _new_session()
        try:
            existing = (
                session.query(ContainerInstanceModel).filter_by(owner_key=identity, challenge_id=challenge_id).first()
            )
            if existing is not None:
                return _reservation(existing, created=False)
        finally:
            session.close()
        raise CreateCapacityUnavailable("could not reserve an instance after concurrent updates")

    @staticmethod
    def mark_running(
        instance_id: str,
        provision_token: str,
        entry_container_id: str,
        *,
        stack_id: str | None = None,
        physical_members: tuple[PhysicalMember, ...],
    ) -> FinalizedInstance | None:
        """Atomically persist every physical member and finalize provisioning."""

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
            )
            session.commit()
            return result
        finally:
            session.close()

    @staticmethod
    def mark_cleanup_pending(instance_id: str, provision_token: str, error: str) -> bool:
        """Fail closed while retaining quota and placement accounting."""

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
                        ContainerInstanceModel.state: "cleanup_pending",
                        ContainerInstanceModel.state_version: ContainerInstanceModel.state_version + 1,
                        # Ambiguous provisioning retains its create slot and
                        # deadline. Reconciliation must prove Docker absence
                        # before delete releases either capacity or owner quota.
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
        """Release quota/accounting only after the caller proved Docker absence."""

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
            # RESTRICT protects against accidental logical deletion. Once the
            # caller has proved Docker absence, remove explicit physical rows
            # and the logical instance in the same transaction.
            session.query(ContainerInfoModel).filter_by(instance_id=instance_id).delete(synchronize_session=False)
            deleted = query.delete(synchronize_session=False)
            session.commit()
            return deleted == 1
        finally:
            session.close()

    @staticmethod
    def release_operation(instance_id: str, operation_token: str, error: str | None = None) -> bool:
        """Release a live operation claim without releasing resource accounting.

        This is used when Docker cleanup failed normally. A process crash is
        recovered by the bounded stale-token takeover in ``claim_operation``.
        """

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
    ) -> str | None:
        """Claim a stop/expiry/reconcile operation using a random fencing token."""

        if not expected_states:
            raise ValueError("at least one expected state is required")
        if target_state != "cleanup_pending":
            raise ValueError("invalid operation target state")
        if stale_after_seconds <= 0:
            raise ValueError("operation lease must be positive")
        token = uuid.uuid4().hex
        session = _new_session()
        try:
            now = time.time()
            updated = (
                session.query(ContainerInstanceModel)
                .filter(
                    ContainerInstanceModel.id == instance_id,
                    ContainerInstanceModel.state.in_(expected_states),
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
        """Atomically mark solved and shorten, but never extend, expiry."""

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
        """Atomically extend a running instance without consuming a renewal."""

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
        """Retry post-solve shortening from CTFd's durable solve records."""

        if shorten_seconds <= 0:
            return 0

        from CTFd.models import Solves

        candidates = ContainerInstanceModel.query.filter_by(state="running", solved_at=None).all()
        reconciled = 0
        for instance in candidates:
            solve_query = Solves.query.filter_by(challenge_id=instance.challenge_id)
            if instance.team_id is not None:
                solve_query = solve_query.filter_by(team_id=instance.team_id)
            elif instance.user_id is not None:
                solve_query = solve_query.filter_by(user_id=instance.user_id)
            else:
                continue
            solve = solve_query.order_by(Solves.date.asc()).first()
            if solve is None or solve.date is None:
                continue
            solved_at = calendar.timegm(solve.date.utctimetuple()) + solve.date.microsecond / 1_000_000
            target_expires = int(solved_at) + shorten_seconds
            if InstanceCoordinator.shorten_after_solve(instance.id, target_expires, solved_at=solved_at):
                reconciled += 1
        return reconciled
