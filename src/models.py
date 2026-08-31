import time

from sqlalchemy.orm import relationship

from CTFd.models import Challenges, db

INSTANCE_STATES = (
    "provisioning",
    "running",
    "cleanup_pending",
)
CONTEXT_STATES = ("active", "draining", "disabled", "retired_orphaned")
CONTEXT_HEALTH_STATES = ("unknown", "healthy", "unhealthy")


class ContainerChallengeModel(Challenges):
    __tablename__ = "container_challenges"
    __mapper_args__ = {"polymorphic_identity": "container"}
    id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), primary_key=True)
    image = db.Column(db.Text)
    port = db.Column(db.Integer)
    command = db.Column(db.Text, default="")
    volumes = db.Column(db.Text, default="")
    ctype = db.Column(db.Text, default="tcp")
    ssh_username = db.Column(db.Text, nullable=True)
    ssh_password = db.Column(db.Text, nullable=True)
    expiration_seconds = db.Column(db.Integer, default=1800)
    max_renewals = db.Column(db.Integer, default=2)
    max_memory_mb = db.Column(db.Integer, nullable=True)
    max_cpu = db.Column(db.Float, nullable=True)
    docker_context = db.Column(db.String(512), nullable=True)
    cap_add = db.Column(db.Text, default="")
    services_json = db.Column(db.Text, nullable=True)
    network_json = db.Column(db.Text, nullable=True)


class ContainerInfoModel(db.Model):
    __tablename__ = "container_info"
    container_id = db.Column(db.String(512), primary_key=True)
    instance_id = db.Column(
        db.String(32),
        db.ForeignKey("container_instances.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="RESTRICT"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    port = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.Integer, nullable=False)
    expires = db.Column(db.Integer, nullable=False)
    renewals_used = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    docker_context = db.Column(db.String(512), nullable=False)
    stack_id = db.Column(db.String(64), nullable=True, index=True)
    is_entry = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    team = relationship("Teams", foreign_keys=[team_id])
    user = relationship("Users", foreign_keys=[user_id])
    challenge = relationship(ContainerChallengeModel, foreign_keys=[challenge_id])
    instance = relationship("ContainerInstanceModel", foreign_keys=[instance_id], back_populates="containers")

    @classmethod
    def entry_or_standalone(cls):
        return cls.is_entry == True  # noqa: E712


class ContainerSettingsModel(db.Model):
    __tablename__ = "container_settings"
    key = db.Column(db.String(512), primary_key=True)
    value = db.Column(db.Text)


class ContainerMaintenanceModel(db.Model):
    __tablename__ = "container_maintenance"
    name = db.Column(db.String(64), primary_key=True)
    last_started = db.Column(db.Float(precision=53), nullable=False, default=0, server_default="0")


class ContainerHistoryModel(db.Model):
    __tablename__ = "container_history"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # Deliberately not a foreign key: logical instances are active-only while
    # history must survive their deletion.
    instance_id = db.Column(db.String(32), nullable=False, index=True)
    container_id = db.Column(db.String(512), nullable=False)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="SET NULL"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    docker_context = db.Column(db.String(512), nullable=False)
    stack_id = db.Column(db.String(64), nullable=True)
    is_entry = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    service_name = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.Float(precision=53), nullable=False)
    stopped_at = db.Column(db.Float(precision=53), nullable=True)
    reason = db.Column(db.String(32), nullable=True)


class DockerContextModel(db.Model):
    __tablename__ = "docker_contexts"
    id = db.Column(db.Integer, primary_key=True)
    context_name = db.Column(db.String(512), unique=True, nullable=False)
    hostname = db.Column(db.String(512), nullable=True)
    pub_hostname = db.Column(db.String(512), nullable=False)
    weight = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    # Active accepts placement; other states retain the endpoint for management
    # and cleanup without admitting new work.
    state = db.Column(db.String(24), nullable=False, default="active", server_default="active")
    placement_version = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    health_state = db.Column(db.String(16), nullable=False, default="unknown", server_default="unknown")
    health_checked_at = db.Column(db.Float(precision=53), nullable=True)
    health_error = db.Column(db.String(512), nullable=True)

    __table_args__ = (
        db.CheckConstraint(
            "state IN ('active', 'draining', 'disabled', 'retired_orphaned')",
            name="ck_docker_context_state",
        ),
        db.CheckConstraint(
            "health_state IN ('unknown', 'healthy', 'unhealthy')",
            name="ck_docker_context_health",
        ),
        db.CheckConstraint("placement_version >= 0", name="ck_docker_context_placement_version"),
    )


class ContainerInstanceModel(db.Model):
    """One active logical challenge instance.

    Physical members live in ``container_info``. This row is removed only after
    Docker cleanup is confirmed, so its uniqueness constraints are the durable
    quota/deduplication boundary across workers and application replicas.

    A context foreign-key ID is used rather than its name so context deletion is
    restricted while resources remain active. Physical/history rows keep the
    context-name snapshot for durable auditing.
    """

    __tablename__ = "container_instances"
    __table_args__ = (
        db.UniqueConstraint("owner_key", "challenge_id", name="uq_container_instance_owner_challenge"),
        db.UniqueConstraint("owner_key", "quota_slot", name="uq_container_instance_owner_quota_slot"),
        db.UniqueConstraint("docker_context_id", "create_slot", name="uq_container_instance_context_create_slot"),
        db.CheckConstraint("quota_slot >= 0", name="ck_container_instance_quota_slot"),
        db.CheckConstraint("create_slot IS NULL OR create_slot >= 0", name="ck_container_instance_create_slot"),
        db.CheckConstraint("placement_units >= 1", name="ck_container_instance_placement_units"),
        db.CheckConstraint("state_version >= 0", name="ck_container_instance_state_version"),
        db.CheckConstraint(
            "state IN ('provisioning', 'running', 'cleanup_pending')",
            name="ck_container_instance_state",
        ),
    )

    id = db.Column(db.String(32), primary_key=True)
    # Non-null, normalized server identity (for example user:12 or team:7).
    owner_key = db.Column(db.String(32), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="RESTRICT"), nullable=False)
    quota_slot = db.Column(db.Integer, nullable=False)
    state = db.Column(db.String(24), nullable=False, default="provisioning", server_default="provisioning")
    state_version = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    docker_context_id = db.Column(
        db.Integer,
        db.ForeignKey("docker_contexts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Held only while Docker provisioning is in flight. Setting it to NULL
    # releases the globally unique per-context create slot.
    create_slot = db.Column(db.Integer, nullable=True)
    placement_units = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    stack_id = db.Column(db.String(64), nullable=True, unique=True)
    entry_container_id = db.Column(db.String(512), nullable=True, unique=True)
    # Immutable fencing identity for every Docker object in this provisioning attempt.
    provision_token = db.Column(db.String(32), nullable=False, unique=True)
    # Mutable ownership token for stop/expiry/reconcile operations.
    operation_token = db.Column(db.String(32), nullable=True)
    provision_deadline = db.Column(db.Float(precision=53), nullable=True)
    created_at = db.Column(db.Float(precision=53), nullable=False, default=time.time)
    updated_at = db.Column(db.Float(precision=53), nullable=False, default=time.time, onupdate=time.time)
    expires = db.Column(db.Integer, nullable=False)
    renewals_used = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    solved_at = db.Column(db.Float(precision=53), nullable=True)
    last_error = db.Column(db.String(512), nullable=True)

    user = relationship("Users", foreign_keys=[user_id])
    team = relationship("Teams", foreign_keys=[team_id])
    challenge = relationship("Challenges", foreign_keys=[challenge_id])
    docker_context = relationship(DockerContextModel, foreign_keys=[docker_context_id])
    containers = relationship(ContainerInfoModel, back_populates="instance", passive_deletes=True)


class ContainerFlagShareModel(db.Model):
    __tablename__ = "container_flag_shares"
    __table_args__ = (
        db.UniqueConstraint(
            "submitter_user_xid",
            "challenge_xid",
            "submitted_token_digest",
            name="uq_flag_share_submitter_digest",
        ),
    )
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="SET NULL"), nullable=True)
    challenge_xid = db.Column(db.String(32), nullable=False)
    submitter_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    submitter_team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    # Immutable submitting-user identity (always ``user:<Users.id>``) and a
    # keyed HMAC-SHA256 digest make deduplication portable without retaining a
    # queryable low-entropy token. Team identity remains audit metadata only.
    submitter_user_xid = db.Column(db.String(32), nullable=False)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    owner_team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    submitted_token_digest = db.Column(db.String(64), nullable=False)
    ip = db.Column(db.String(46), nullable=True)
    timestamp = db.Column(db.Float(precision=53), index=True)

    submitter_user = relationship("Users", foreign_keys=[submitter_user_id])
    submitter_team = relationship("Teams", foreign_keys=[submitter_team_id])
    owner_user = relationship("Users", foreign_keys=[owner_user_id])
    owner_team = relationship("Teams", foreign_keys=[owner_team_id])
    challenge = relationship("Challenges", foreign_keys=[challenge_id])
