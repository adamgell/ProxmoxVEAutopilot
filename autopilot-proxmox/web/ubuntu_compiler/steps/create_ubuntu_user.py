"""create_ubuntu_user: emit a sudo-enabled user into autoinstall user-data."""
from __future__ import annotations

import crypt

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

    salt = crypt.mksalt(crypt.METHOD_SHA512)
    hashed = crypt.crypt(password, salt)

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
