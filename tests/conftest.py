import html as _html
import importlib.util
import sys
import types
from unittest.mock import MagicMock
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def aggressive_thread_switching():
    original = sys.getswitchinterval()
    sys.setswitchinterval(0.000001)
    yield
    sys.setswitchinterval(original)


# stub out all external dependencies before any plugin code is imported

_ctfd_models = types.ModuleType("CTFd.models")
_ctfd_models.db = MagicMock()
_ctfd_models.Challenges = type("Challenges", (), {})
_ctfd_models.Users = MagicMock()
_ctfd_models.Teams = MagicMock()
_ctfd_models.Flags = MagicMock()
_ctfd_models.Solves = MagicMock()

_stub_modules = [
    "CTFd",
    "CTFd.plugins",
    "CTFd.plugins.challenges",
    "CTFd.plugins.challenges.decay",
    "CTFd.exceptions",
    "CTFd.exceptions.challenges",
    "CTFd.utils",
    "CTFd.utils.decorators",
    "CTFd.utils.user",
    "flask",
    "docker",
    "docker.errors",
    "paramiko",
    "paramiko.ssh_exception",
    "requests",
    "requests.exceptions",
    "markupsafe",
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.background",
    "sqlalchemy",
    "sqlalchemy.orm",
    "sqlalchemy.exc",
    "gevent",
    "gevent.monkey",
    "gevent.threadpool",
]

for mod_name in _stub_modules:
    sys.modules[mod_name] = types.ModuleType(mod_name)

sys.modules["CTFd.models"] = _ctfd_models

# markupsafe stub with a real escape implementation
_markupsafe = sys.modules["markupsafe"]
_markupsafe.escape = lambda s: _html.escape(str(s), quote=True)

# flask stubs
_flask = sys.modules["flask"]
for attr in (
    "Blueprint",
    "Flask",
    "Request",
    "request",
    "jsonify",
    "render_template",
    "Response",
    "stream_with_context",
    "current_app",
):
    setattr(_flask, attr, MagicMock())

# CTFd decorator stubs
_decorators = sys.modules["CTFd.utils.decorators"]
_decorators.authed_only = lambda f: f
_decorators.admins_only = lambda f: f
_decorators.during_ctf_time_only = lambda f: f
_decorators.require_verified_emails = lambda f: f
_decorators.ratelimit = lambda **_kw: lambda f: f

_user_utils = sys.modules["CTFd.utils.user"]
_user_utils.get_current_user = MagicMock()
_user_utils.get_ip = MagicMock(return_value="127.0.0.1")

_ctfd_utils = sys.modules["CTFd.utils"]
_ctfd_utils.get_config = MagicMock(return_value="users")

_plugins = sys.modules["CTFd.plugins"]
_plugins.register_plugin_assets_directory = MagicMock()
_plugins.register_admin_plugin_menu_bar = MagicMock()

_challenges = sys.modules["CTFd.plugins.challenges"]
_challenges.BaseChallenge = type(
    "BaseChallenge", (), {"attempt": staticmethod(lambda challenge, request: (False, "incorrect"))}
)
_challenges.CHALLENGE_CLASSES = {}
_challenges.calculate_value = MagicMock()

_challenges_decay = sys.modules["CTFd.plugins.challenges.decay"]
_challenges_decay.DECAY_FUNCTIONS = {"linear": MagicMock(), "logarithmic": MagicMock()}

_chal_exc = sys.modules["CTFd.exceptions.challenges"]
_chal_exc.ChallengeCreateException = type("ChallengeCreateException", (Exception,), {})
_chal_exc.ChallengeUpdateException = type("ChallengeUpdateException", (Exception,), {})

_flags_mod = types.ModuleType("CTFd.plugins.flags")
_flags_mod.BaseFlag = type("BaseFlag", (), {"name": "static", "templates": {}})
_flags_mod.FLAG_CLASSES = {}
sys.modules["CTFd.plugins.flags"] = _flags_mod

# docker stubs
_docker = sys.modules["docker"]
_docker.from_env = MagicMock()
_docker.DockerClient = MagicMock()
_docker_errors = sys.modules["docker.errors"]
_docker_errors.DockerException = type("DockerException", (Exception,), {})
_docker_errors.NotFound = type("NotFound", (Exception,), {})
_docker_errors.ImageNotFound = type("ImageNotFound", (Exception,), {})
_docker_errors.APIError = type("APIError", (Exception,), {})
_docker.errors = _docker_errors

_docker_models = types.ModuleType("docker.models")
_docker_models_containers = types.ModuleType("docker.models.containers")
_docker_models_containers.Container = MagicMock()
_docker_models_networks = types.ModuleType("docker.models.networks")
_docker_models_networks.Network = MagicMock()
_docker_models.containers = _docker_models_containers
_docker_models.networks = _docker_models_networks
_docker.models = _docker_models
sys.modules["docker.models"] = _docker_models
sys.modules["docker.models.containers"] = _docker_models_containers
sys.modules["docker.models.networks"] = _docker_models_networks

_docker_types = types.ModuleType("docker.types")
_docker_types.IPAMPool = MagicMock()
_docker_types.IPAMConfig = MagicMock()
_docker.types = _docker_types
sys.modules["docker.types"] = _docker_types

# paramiko stubs
_paramiko = sys.modules["paramiko"]
_paramiko_ssh = sys.modules["paramiko.ssh_exception"]
_paramiko_ssh.SSHException = type("SSHException", (Exception,), {})
_paramiko.ssh_exception = _paramiko_ssh

# requests stubs
_requests = sys.modules["requests"]
_requests_exc = sys.modules["requests.exceptions"]
_requests_exc.RequestException = type("RequestException", (Exception,), {})
_requests.exceptions = _requests_exc

# apscheduler stubs
_apscheduler_sched = sys.modules["apscheduler.schedulers"]
_apscheduler_sched.SchedulerNotRunningError = type("SchedulerNotRunningError", (Exception,), {})
_apscheduler_bg = sys.modules["apscheduler.schedulers.background"]
_apscheduler_bg.BackgroundScheduler = MagicMock()

# sqlalchemy stubs
_sqlalchemy_orm = sys.modules["sqlalchemy.orm"]
_sqlalchemy_orm.relationship = MagicMock()
sys.modules["sqlalchemy"].orm = _sqlalchemy_orm
_sqlalchemy_exc = sys.modules["sqlalchemy.exc"]
_sqlalchemy_exc.IntegrityError = type("IntegrityError", (Exception,), {})
sys.modules["sqlalchemy"].exc = _sqlalchemy_exc

# gevent stub: ThreadPool.apply runs the callable synchronously so tests
# exercise the wrapped code paths without needing a real hub
_gevent = sys.modules["gevent"]
_gevent_threadpool = sys.modules["gevent.threadpool"]
_gevent_monkey = sys.modules["gevent.monkey"]


class _StubThreadPool:
    def __init__(self, maxsize=None):
        pass

    def apply(self, fn, args=None, kwds=None):
        return fn(*(args or ()), **(kwds or {}))


_gevent_threadpool.ThreadPool = _StubThreadPool
_gevent_monkey.is_module_patched = lambda name: True
_gevent.threadpool = _gevent_threadpool
_gevent.monkey = _gevent_monkey

# register src/ as a package so relative imports resolve
src_dir = Path(__file__).resolve().parent.parent / "src"
src_dir_str = str(src_dir)

PKG = "_cc_plugin"

pkg = types.ModuleType(PKG)
pkg.__path__ = [src_dir_str]
pkg.__package__ = PKG
pkg.__file__ = str(src_dir / "__init__.py")
sys.modules[PKG] = pkg


def _load_module(name, filepath=None):
    full = f"{PKG}.{name}"
    if filepath is None:
        filepath = src_dir / f"{name}.py"
    spec = importlib.util.spec_from_file_location(full, filepath)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = PKG
    sys.modules[full] = mod
    sys.modules[name] = mod
    setattr(pkg, name, mod)
    spec.loader.exec_module(mod)
    return mod


def _load_subpackage(name):
    subpkg_dir = src_dir / name
    full = f"{PKG}.{name}"

    init_file = subpkg_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(full, init_file, submodule_search_locations=[str(subpkg_dir)])
    init_mod = importlib.util.module_from_spec(spec)
    init_mod.__path__ = [str(subpkg_dir)]
    init_mod.__package__ = full
    sys.modules[full] = init_mod
    sys.modules[name] = init_mod
    setattr(pkg, name, init_mod)

    # pre-register child modules so relative imports from __init__ resolve,
    # __init__ defines containers_bp first then imports children, each child
    # imports containers_bp back from __init__ which works because __init__
    # has partially executed by that point (normal python import behavior)
    for child in sorted(subpkg_dir.glob("*.py")):
        if child.name == "__init__.py":
            continue
        child_name = child.stem
        child_full = f"{full}.{child_name}"
        child_spec = importlib.util.spec_from_file_location(child_full, child)
        child_mod = importlib.util.module_from_spec(child_spec)
        child_mod.__package__ = full
        sys.modules[child_full] = child_mod

    spec.loader.exec_module(init_mod)

    # exec child modules that were pre-registered but not yet loaded
    for child in sorted(subpkg_dir.glob("*.py")):
        if child.name == "__init__.py":
            continue
        child_full = f"{full}.{child.stem}"
        child_mod = sys.modules.get(child_full)
        if child_mod and not getattr(child_mod, "__loaded__", False):
            child_spec = child_mod.__spec__
            if child_spec and child_spec.loader:
                child_spec.loader.exec_module(child_mod)
                child_mod.__loaded__ = True


# load in dependency order
_load_module("models")
_load_module("event_logger")
_load_module("event_bus")
_load_module("utils")
_load_module("freshness")
_load_module("exceptions")
_load_module("docker_host_manager")
_load_module("orchestrator")
_load_module("container_manager")
_load_module("flag_type")
_load_module("challenges")
_load_subpackage("views")

# pytest tries to import __init__ as a standalone module from rootdir
sys.modules["__init__"] = types.ModuleType("__init__")
