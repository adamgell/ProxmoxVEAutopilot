"""create_ubuntu_user step compiler."""
from __future__ import annotations

from passlib.hash import sha512_crypt

import pytest

from web.ubuntu_compiler import compile_step, UbuntuCompileError


def test_emits_users_block_with_hashed_password() -> None:
    creds = {
        42: {"username": "acgell", "password": "s3cret!"},
    }
    out = compile_step(
        "create_ubuntu_user",
        params={"local_admin_credential_id": 42},
        credentials=creds,
    )
    users = out.autoinstall_body["user-data"]["users"]
    assert isinstance(users, list) and len(users) == 1
    u = users[0]
    assert u["name"] == "acgell"
    assert u["lock_passwd"] is False
    assert u["shell"] == "/bin/bash"
    assert "sudo" in u["groups"]
    # SHA-512 passwd hash starts with $6$
    assert u["passwd"].startswith("$6$")
    # Verify the hash matches the plaintext.
    assert sha512_crypt.verify("s3cret!", u["passwd"])


def test_missing_credential_raises() -> None:
    with pytest.raises(UbuntuCompileError):
        compile_step(
            "create_ubuntu_user",
            params={"local_admin_credential_id": 999},
            credentials={},
        )


def test_missing_credential_id_param_raises() -> None:
    with pytest.raises(UbuntuCompileError):
        compile_step("create_ubuntu_user", params={}, credentials={})
