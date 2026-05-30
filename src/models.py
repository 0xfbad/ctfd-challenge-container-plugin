from sqlalchemy.orm import relationship
from CTFd.models import db, Challenges


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
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"))
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    port = db.Column(db.Integer)
    timestamp = db.Column(db.Integer)
    expires = db.Column(db.Integer)
    renewals_used = db.Column(db.Integer, default=0)
    docker_context = db.Column(db.String(512), nullable=True)
    stack_id = db.Column(db.String(64), nullable=True, index=True)
    is_entry = db.Column(db.Boolean, default=True)
    team = relationship("Teams", foreign_keys=[team_id])
    user = relationship("Users", foreign_keys=[user_id])
    challenge = relationship(ContainerChallengeModel, foreign_keys=[challenge_id])

    @classmethod
    def entry_or_standalone(cls):
        # filter: rows representing an entry container of a stack, or a standalone
        # container with no stack at all. excludes companion rows
        return db.or_(cls.is_entry == True, cls.stack_id.is_(None))  # noqa: E712


class ContainerSettingsModel(db.Model):
    __tablename__ = "container_settings"
    key = db.Column(db.String(512), primary_key=True)
    value = db.Column(db.Text)


class ContainerHistoryModel(db.Model):
    __tablename__ = "container_history"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    container_id = db.Column(db.String(512))
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="SET NULL"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    docker_context = db.Column(db.String(512), nullable=True)
    stack_id = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.Float(precision=53))
    stopped_at = db.Column(db.Float(precision=53), nullable=True)
    reason = db.Column(db.String(32), nullable=True)


class DockerContextModel(db.Model):
    __tablename__ = "docker_contexts"
    id = db.Column(db.Integer, primary_key=True)
    context_name = db.Column(db.String(512), unique=True, nullable=False)
    hostname = db.Column(db.String(512), nullable=True)
    pub_hostname = db.Column(db.String(512), nullable=False)
    weight = db.Column(db.Integer, default=1)
    enabled = db.Column(db.Boolean, default=True)


class ContainerFlagShareModel(db.Model):
    __tablename__ = "container_flag_shares"
    # submitted_token is part of the PK-equivalent uniqueness because a single submitter
    # might try multiple owners' tokens against the same challenge, but the same submitter
    # double-clicking submit of the same token is just one logical incident
    __table_args__ = (
        db.UniqueConstraint(
            "submitter_user_id",
            "submitter_team_id",
            "challenge_id",
            "submitted_token",
            name="uq_flag_share_submitter_token",
        ),
    )
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="SET NULL"), nullable=True)
    submitter_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    submitter_team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    owner_team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    # MySQL/MariaDB cannot index a TEXT column without a key length, so cap at 191 chars
    # to stay under the utf8mb4 single-key 767-byte limit while still fitting any real token
    submitted_token = db.Column(db.String(191), nullable=True)
    ip = db.Column(db.String(46), nullable=True)
    timestamp = db.Column(db.Float(precision=53), index=True)

    submitter_user = relationship("Users", foreign_keys=[submitter_user_id])
    submitter_team = relationship("Teams", foreign_keys=[submitter_team_id])
    owner_user = relationship("Users", foreign_keys=[owner_user_id])
    owner_team = relationship("Teams", foreign_keys=[owner_team_id])
    challenge = relationship("Challenges", foreign_keys=[challenge_id])
