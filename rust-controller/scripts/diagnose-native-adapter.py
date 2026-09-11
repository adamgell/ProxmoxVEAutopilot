#!/usr/bin/env python3
"""Read-only CI metadata for diagnosing the native adapter's trust rejection.

This does not authorize execution or replace AdapterRegistry's checks. It reads
only the fixed adapter entrypoint, its absolute shebang path, and file metadata.
"""

import os
from pathlib import Path
import stat
import sys


def describe(label, path):
    canonical = path.resolve(strict=True)
    print(f"{label}: requested={path} canonical={canonical}")
    for ancestor in (canonical, *canonical.parents):
        metadata = ancestor.stat()
        print(
            f"  {ancestor}: uid={metadata.st_uid} gid={metadata.st_gid} "
            f"mode={stat.S_IMODE(metadata.st_mode):04o}"
        )
    return canonical


def main():
    print(f"adapter diagnostic: platform={sys.platform} effective_uid={os.geteuid()}")
    root = Path(__file__).resolve().parents[2]
    playbook = root / "autopilot-proxmox/playbooks/_test_long_sleep.yml"
    print(f"playbook: requested={playbook} canonical={playbook.resolve(strict=True)}")
    executable = Path(
        "/opt/homebrew/bin/ansible-playbook"
        if sys.platform == "darwin" else "/usr/bin/ansible-playbook"
    )
    canonical = describe("entrypoint", executable)
    with canonical.open("rb") as source:
        line = source.readline(4096).decode("utf-8", errors="replace").rstrip("\n")
    interpreter = line.removeprefix("#!")
    # Never print arbitrary entrypoint contents; only a simple absolute path.
    if line.startswith("#!/") and not any(c.isspace() for c in interpreter):
        describe("shebang interpreter", Path(interpreter))
        print(f"shebang basename starts python3: {Path(interpreter).name.startswith('python3')}")
    else:
        print("shebang rejected: not a simple absolute interpreter path")


if __name__ == "__main__":
    main()
