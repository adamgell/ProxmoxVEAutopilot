import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class CoordinationTests(unittest.TestCase):
    def test_finished_selection_is_resumed_and_only_confirmed_paused_worker_is_killed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text(r"""#!/usr/bin/env python3
import os, pathlib, sys
root = pathlib.Path(os.environ['PROOF_STUB_ROOT'])
args = sys.argv[1:]
with (root / 'calls').open('a') as log:
    log.write(' '.join(args[4:]) + '\n')
if args[-1] in ('cancel-and-select', 'select-running'):
    print('worker-1')
if 'confirm-paused-running' in args:
    count = root / 'confirmed'
    if not count.exists():
        count.touch()
        sys.exit(3)
""")
            docker.chmod(0o755)
            result = subprocess.run(
                ["bash", str(Path(__file__).with_name("compose-proof.sh"))],
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "PROOF_STUB_ROOT": str(root)},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / "calls").read_text().splitlines()
            actions = [line for line in calls if any(token in line for token in
                       ("pause worker", "confirm-paused-running", "kill -s"))]
            self.assertEqual(actions, [
                "docker-compose.test.yml pause worker-1",
                "docker-compose.test.yml run --rm -T proof confirm-paused-running worker-1",
                "docker-compose.test.yml unpause worker-1",
                "docker-compose.test.yml pause worker-1",
                "docker-compose.test.yml run --rm -T proof confirm-paused-running worker-1",
                "docker-compose.test.yml kill -s SIGKILL worker-1",
            ])
