import shutil
import subprocess
from pathlib import Path


def test_session_recovery_transport_behavior():
    node = shutil.which('node')
    assert node, 'Node is required for session recovery regression tests'
    result = subprocess.run(
        [node, '--test', str(Path(__file__).with_name('session_recovery.test.cjs'))],
        capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
