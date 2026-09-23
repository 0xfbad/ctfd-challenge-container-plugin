from typing import Final

HOSTS_BUSY: Final = "all container hosts are busy, please try again shortly"
HOST_UNREACHABLE: Final = "the container host is temporarily unreachable, please wait"
CONTEXT_UNAVAILABLE: Final = "container context is unavailable; cleanup will retry automatically"
HOST_UNAVAILABLE_CLEANUP: Final = "container host unavailable; cleanup will be retried"
PLACEMENT_FAILED: Final = "container placement failed"


QUOTA_EXCEEDED: Final = "you can only spawn {maximum} containers at a time, please stop other containers"
REQUEST_IN_PROGRESS: Final = "another container request is in progress, please wait"
RATE_LIMITED: Final = "Too many requests. Limit is {limit} requests in {interval} seconds"
NO_RENEWALS: Final = "no renewals remaining"
SOLVED_NO_RENEW: Final = "solved containers cannot be renewed"
EXPIRED_NO_RENEW: Final = "expired containers cannot be renewed"
CHALLENGE_LOCKED: Final = "challenge locked"


CLEANUP_IN_PROGRESS: Final = "Container cleanup is in progress. Please try again in a few minutes."
START_TIMEOUT: Final = "Container creation timed out. Please try again in a few minutes."
CLEANUP_PENDING: Final = "container cleanup is pending"
CLEANUP_ALREADY_RUNNING: Final = "container cleanup is already in progress"
CLEANUP_FINALIZING: Final = "container cleanup finalization is already in progress"
AWAITING_CLEANUP: Final = "The previous instance is awaiting confirmed cleanup."
PORT_UNAVAILABLE: Final = "could not determine container port"
FINALIZATION_FAILED: Final = "database finalization failed; container cleanup has been scheduled"


CONTAINER_NOT_FOUND: Final = "container not found"
CONTAINER_NOT_FOUND_RESET: Final = "container not found, try resetting the container"
INSTANCE_NOT_FOUND: Final = "container instance not found"
NO_CONTAINER: Final = "no container found"
CHALLENGE_NOT_FOUND: Final = "challenge not found"
INVALID_REQUEST: Final = "invalid request"
MISSING_FIELD: Final = "no {field} specified"
INVALID_CHALLENGE_ID: Final = "invalid challenge id"
USER_NOT_FOUND: Final = "user not found"
TEAM_REQUIRED: Final = "user not a member of a team"
TEAM_REQUIRED_FLAG: Final = "you must be on a team to submit flags"
MEMORY_LIMIT_INVALID: Final = "memory limit must be an integer"
CPU_LIMIT_INVALID: Final = "cpu limit must be a positive number"


SERVER_ERROR: Final = "a server error occurred, please try again"
IMAGE_NOT_FOUND: Final = "docker image not found"
CHALLENGE_MISCONFIGURED: Final = "This challenge has a broken configuration. This is on our end, not yours."
CHALLENGE_UNAVAILABLE: Final = (
    "This challenge is temporarily unavailable due to a server configuration issue. This is on our end, not yours."
)
FLAG_NOT_YOURS: Final = "this flag belongs to another participant. this attempt has been logged."
