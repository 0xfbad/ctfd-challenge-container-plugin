import subprocess
import sys
from pathlib import Path


def test_real_sqlite_concurrency_and_lifecycle_cas(tmp_path):
    script = Path(__file__).with_name("_coordination_sqlite_smoke.py")
    subprocess.run(
        [sys.executable, str(script), str(tmp_path / "coordination.sqlite")],
        check=True,
        text=True,
        capture_output=True,
    )
