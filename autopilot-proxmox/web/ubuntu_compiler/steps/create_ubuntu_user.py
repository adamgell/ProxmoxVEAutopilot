"""create_ubuntu_user: emit a sudo-enabled user at the top level of cloud-config."""
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

    # sha512_crypt produces a self-salting $6$... hash accepted by cloud-init.
    # passlib is pure-Python so it works on Python 3.13+ where the stdlib
    # `crypt` module has been removed.
    hashed = sha512_crypt.hash(password)

    # Top-level `users:` on a cloud image replaces the default `ubuntu` user.
    # For a managed workstation template we want our admin to BE the primary
    # user, so we omit `"default"` from the list. If someone needs to keep
    # the default ubuntu user alongside, prepend "default" to this list.
    return StepOutput(
        cloud_config={
            "users": [
                {
                    "name": username,
                    "passwd": hashed,
                    "groups": ["sudo"],
                    "shell": "/bin/bash",
                    "lock_passwd": False,
                    "sudo": "ALL=(ALL) NOPASSWD:ALL",
                }
            ]
        }
    )
