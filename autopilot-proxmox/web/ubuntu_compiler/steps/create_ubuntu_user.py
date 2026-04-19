"""create_ubuntu_user: emit a sudo-enabled user into autoinstall user-data."""
from __future__ import annotations

from passlib.hash import sha512_crypt

from ..registry import register
from ..types import StepOutput, UbuntuCompileError


@register("create_ubuntu_user")
def compile_create_ubuntu_user(params, credentials) -> StepOutput:
    cred_id = params.get("local_admin_credential_id")
    if cred_id is None:
        raise UbuntuCompileError(
            "create_ubuntu_user: params.local_admin_credential_id is required"
        )
    cred = credentials.get(cred_id)
    if cred is None:
        raise UbuntuCompileError(
            f"create_ubuntu_user: credential {cred_id} not provided"
        )
    username = cred.get("username")
    password = cred.get("password")
    if not username or not password:
        raise UbuntuCompileError(
            "create_ubuntu_user: credential missing username or password"
        )

    # sha512_crypt produces a self-salting $6$... hash accepted by cloud-init /
    # Ubuntu autoinstall user-data. `passlib` is pure-Python so it works on
    # Python 3.13+ where the stdlib `crypt` module has been removed.
    hashed = sha512_crypt.hash(password)

    return StepOutput(
        autoinstall_body={
            "user-data": {
                "users": [
                    {
                        "name": username,
                        "passwd": hashed,
                        "groups": ["sudo"],
                        "shell": "/bin/bash",
                        "lock_passwd": False,
                    }
                ]
            }
        }
    )
