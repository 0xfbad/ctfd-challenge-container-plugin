"""Standalone real-SQLAlchemy concurrency/CAS smoke test."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, relationship

ROOT = Path(__file__).resolve().parents[1]
Base = declarative_base()


class DockerContextModel(Base):
    __tablename__ = "docker_contexts"
    id = sa.Column(sa.Integer, primary_key=True)
    context_name = sa.Column(sa.String(512), nullable=False, unique=True)
    state = sa.Column(sa.String(24), nullable=False)
    health_state = sa.Column(sa.String(16), nullable=False)
    weight = sa.Column(sa.Integer, nullable=False)
    placement_version = sa.Column(sa.Integer, nullable=False, default=0)


class ContainerInstanceModel(Base):
    __tablename__ = "container_instances"
    __table_args__ = (
        sa.UniqueConstraint("owner_key", "challenge_id"),
        sa.UniqueConstraint("owner_key", "quota_slot"),
        sa.UniqueConstraint("docker_context_id", "create_slot"),
    )
    id = sa.Column(sa.String(32), primary_key=True)
    owner_key = sa.Column(sa.String(32), nullable=False)
    user_id = sa.Column(sa.Integer)
    team_id = sa.Column(sa.Integer)
    challenge_id = sa.Column(sa.Integer, nullable=False)
    quota_slot = sa.Column(sa.Integer, nullable=False)
    state = sa.Column(sa.String(24), nullable=False)
    state_version = sa.Column(sa.Integer, nullable=False)
    docker_context_id = sa.Column(sa.Integer, sa.ForeignKey("docker_contexts.id"))
    create_slot = sa.Column(sa.Integer)
    placement_units = sa.Column(sa.Integer, nullable=False)
    stack_id = sa.Column(sa.String(64))
    entry_container_id = sa.Column(sa.String(512))
    provision_token = sa.Column(sa.String(32), nullable=False, unique=True)
    operation_token = sa.Column(sa.String(32))
    provision_deadline = sa.Column(sa.Float)
    created_at = sa.Column(sa.Float, nullable=False)
    updated_at = sa.Column(sa.Float, nullable=False)
    expires = sa.Column(sa.Integer, nullable=False)
    renewals_used = sa.Column(sa.Integer, nullable=False)
    solved_at = sa.Column(sa.Float)
    last_error = sa.Column(sa.String(512))
    docker_context = relationship(DockerContextModel)


class ContainerInfoModel(Base):
    __tablename__ = "container_info"
    container_id = sa.Column(sa.String(512), primary_key=True)
    instance_id = sa.Column(sa.String(32), sa.ForeignKey("container_instances.id"))
    challenge_id = sa.Column(sa.Integer)
    team_id = sa.Column(sa.Integer)
    user_id = sa.Column(sa.Integer)
    port = sa.Column(sa.Integer)
    timestamp = sa.Column(sa.Integer)
    expires = sa.Column(sa.Integer)
    renewals_used = sa.Column(sa.Integer)
    docker_context = sa.Column(sa.String(512))
    stack_id = sa.Column(sa.String(64))
    is_entry = sa.Column(sa.Boolean)


class ContainerHistoryModel(Base):
    __tablename__ = "container_history"
    id = sa.Column(sa.Integer, primary_key=True)
    instance_id = sa.Column(sa.String(32))
    container_id = sa.Column(sa.String(512))
    challenge_id = sa.Column(sa.Integer)
    user_id = sa.Column(sa.Integer)
    team_id = sa.Column(sa.Integer)
    docker_context = sa.Column(sa.String(512))
    stack_id = sa.Column(sa.String(64))
    is_entry = sa.Column(sa.Boolean)
    service_name = sa.Column(sa.String(128))
    created_at = sa.Column(sa.Float)
    stopped_at = sa.Column(sa.Float)
    reason = sa.Column(sa.String(32))


def _load_coordination(engine):
    package = types.ModuleType("cc_runtime")
    package.__path__ = [str(ROOT / "src")]
    sys.modules["cc_runtime"] = package
    model_module = types.ModuleType("cc_runtime.models")
    for model in (ContainerInstanceModel, ContainerInfoModel, ContainerHistoryModel, DockerContextModel):
        setattr(model_module, model.__name__, model)
    sys.modules["cc_runtime.models"] = model_module

    ctfd = types.ModuleType("CTFd")
    ctfd_models = types.ModuleType("CTFd.models")
    ctfd_models.db = types.SimpleNamespace(engine=engine)
    sys.modules["CTFd"] = ctfd
    sys.modules["CTFd.models"] = ctfd_models

    path = ROOT / "src" / "coordination.py"
    spec = importlib.util.spec_from_file_location("cc_runtime.coordination", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(database_path: str) -> None:
    engine = sa.create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            DockerContextModel.__table__.insert(),
            [
                {
                    "id": 1,
                    "context_name": "host-a",
                    "state": "active",
                    "health_state": "healthy",
                    "weight": 1,
                    "placement_version": 0,
                }
            ],
        )

    module = _load_coordination(engine)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def reserve_same_owner():
        try:
            barrier.wait()
            results.append(
                module.InstanceCoordinator().reserve_instance(
                    challenge_id=10,
                    xid=1,
                    is_team=False,
                    submitter_user_id=1,
                    max_instances=2,
                    max_concurrent_creates=2,
                    placement_units=1,
                    expires=1000,
                )
            )
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=reserve_same_owner) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(results) == 2
    assert len({result.instance_id for result in results}) == 1
    assert sorted(result.created for result in results) == [False, True]

    reservation = results[0]
    member = module.PhysicalMember("entry", 30001, True, "entry")
    assert module.InstanceCoordinator.mark_running(
        reservation.instance_id,
        reservation.provision_token,
        "entry",
        physical_members=(member,),
    )
    renewal = module.InstanceCoordinator.renew(reservation.instance_id, now=500, new_expires=1500, max_renewals=2)
    assert renewal is not None and renewal.expires == 1500 and renewal.renewals_used == 1
    solved = module.InstanceCoordinator.shorten_after_solve(reservation.instance_id, 800, solved_at=600.0)
    assert solved is not None and solved.expires == 800 and solved.solved_at == 600.0

    token1 = module.InstanceCoordinator.claim_operation(
        reservation.instance_id, ("running",), "cleanup_pending", stale_after_seconds=1
    )
    assert token1 is not None
    assert (
        module.InstanceCoordinator.claim_operation(
            reservation.instance_id, ("cleanup_pending",), "cleanup_pending", stale_after_seconds=1
        )
        is None
    )
    with engine.begin() as connection:
        connection.execute(
            ContainerInstanceModel.__table__.update()
            .where(ContainerInstanceModel.id == reservation.instance_id)
            .values(updated_at=time.time() - 5)
        )
    token2 = module.InstanceCoordinator.claim_operation(
        reservation.instance_id, ("cleanup_pending",), "cleanup_pending", stale_after_seconds=1
    )
    assert token2 is not None and token2 != token1
    assert module.InstanceCoordinator.delete_after_confirmed_cleanup(
        reservation.instance_id, operation_token=token2, reason="stopped"
    )

    with engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(ContainerInstanceModel)).scalar_one() == 0
        assert connection.execute(sa.select(sa.func.count()).select_from(ContainerInfoModel)).scalar_one() == 0
        history = connection.execute(sa.select(ContainerHistoryModel)).mappings().one()
    assert history["reason"] == "solved" and history["stopped_at"] is not None


if __name__ == "__main__":
    main(sys.argv[1])
