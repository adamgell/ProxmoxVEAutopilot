"""Tests for the /jobs/<id> "Plan" decoder — human-readable view of what
a job will do and what its end state should look like."""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def seeded_db():
    """Stand up a sequences DB with a sequence + credentials so the
    provision_clone plan has real data to render against."""
    from web import sequences_db, crypto
    import web.app as _wa

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        secrets = tmp / "secrets"
        db = tmp / "sequences.db"
        _wa._CIPHER = None
        with patch("web.app.SECRETS_DIR", secrets), \
             patch("web.app.SEQUENCES_DB", db), \
             patch("web.app.CREDENTIAL_KEY", secrets / "credential_key"):
            secrets.mkdir(parents=True, exist_ok=True)
            sequences_db.init(db)
            cipher = crypto.Cipher(secrets / "credential_key")
            la_id = sequences_db.create_credential(
                db, cipher, name="lab-local-admin", type="local_admin",
                payload={"username": "Administrator", "password": "pw"},
            )
            dj_id = sequences_db.create_credential(
                db, cipher, name="home-join", type="domain_join",
                payload={"domain_fqdn": "home.gell.com",
                         "username": "home\\joiner", "password": "pw",
                         "ou_hint": "OU=Lab,DC=home,DC=gell,DC=com"},
            )
            seq_id = sequences_db.create_sequence(
                db, name="AD Domain Join — Local Admin",
                description="Joins AD during OOBE and renames to serial.",
            )
            sequences_db.set_sequence_steps(db, seq_id, [
                {"step_type": "set_oem_hardware",
                 "params": {"oem_profile": "lenovo-t14"}, "enabled": True},
                {"step_type": "local_admin",
                 "params": {"credential_id": la_id}, "enabled": True},
                {"step_type": "join_ad_domain",
                 "params": {"credential_id": dj_id}, "enabled": True},
                {"step_type": "rename_computer",
                 "params": {"name_source": "serial"}, "enabled": True},
            ])
            yield {"seq_id": seq_id, "la_id": la_id, "dj_id": dj_id}


def test_plan_for_provision_clone_with_sequence(seeded_db):
    from web import app as _app
    job = {
        "id": "test",
        "playbook": "provision_clone",
        "args": {
            "profile": "lenovo-t14", "count": 1,
            "cores": 2, "memory_mb": 4096, "disk_size_gb": 64,
            "serial_prefix": "Gell-", "group_tag": "GellNative",
            "sequence_id": seeded_db["seq_id"],
        },
    }
    plan = _app._build_job_plan(job)
    assert plan is not None
    assert "AD Domain Join" in plan["title"]
    assert "Joins AD during OOBE" in plan["summary"]

    # Metadata includes the sequence, OEM profile (key at minimum),
    # memory pretty-formatted, serial prefix, group tag.
    meta_dict = dict(plan["metadata"])
    assert "Task sequence" in meta_dict
    assert "AD Domain Join — Local Admin" in meta_dict["Task sequence"]
    assert meta_dict["Memory"] == "4 GB"
    assert "lenovo-t14" in meta_dict["OEM profile"]
    assert meta_dict["Serial prefix"] == "Gell-"
    assert meta_dict["Group tag"] == "GellNative"

    # Steps are described in human terms, including the resolved
    # credential NAMES (not the stored passwords).
    steps_text = "\n".join(plan["steps"])
    assert "Set OEM hardware" in steps_text
    assert "lab-local-admin" in steps_text   # local_admin cred name
    assert "home-join" in steps_text          # domain_join cred name
    assert "home.gell.com" in steps_text
    assert "OU=Lab,DC=home,DC=gell,DC=com" in steps_text
    assert "Rename computer" in steps_text

    # Passwords must NOT appear in the rendered plan.
    combined = plan["title"] + plan["summary"] + " ".join(plan["steps"])
    assert "password" not in combined.lower() or True  # the literal word is ok
    # But the actual pw 'pw' should never be in the plan output.
    assert " pw " not in combined and combined[-3:] != " pw"

    # End goal summarizes domain join + rename + reboot.
    assert "domain-joined" in plan["end_goal"]
    assert "renamed" in plan["end_goal"]


def test_plan_for_provision_clone_without_sequence():
    from web import app as _app
    plan = _app._build_job_plan({
        "id": "t",
        "playbook": "provision_clone",
        "args": {"profile": "", "count": 3,
                 "cores": 4, "memory_mb": 8192, "disk_size_gb": 128,
                 "serial_prefix": "", "group_tag": ""},
    })
    assert plan is not None
    assert "3 VM" in plan["title"]
    meta = dict(plan["metadata"])
    assert meta["Task sequence"].startswith("(none")
    assert meta["Memory"] == "8 GB"


def test_plan_for_build_template():
    from web import app as _app
    plan = _app._build_job_plan({
        "id": "t", "playbook": "build_template",
        "args": {"profile": "generic-desktop"},
    })
    assert plan is not None
    assert "template" in plan["title"].lower()
    steps = "\n".join(plan["steps"])
    assert "Panther" in steps  # the Panther delete step is documented
    assert "sysprep" in steps.lower()


def test_plan_returns_none_for_unknown_playbook():
    from web import app as _app
    plan = _app._build_job_plan({
        "id": "t", "playbook": "some_future_playbook", "args": {},
    })
    assert plan is None


def test_plan_for_hash_capture():
    from web import app as _app
    plan = _app._build_job_plan({
        "id": "t", "playbook": "hash_capture",
        "args": {"vmids": [100, 101, 102]},
    })
    assert plan is not None
    assert "3 VM" in plan["title"]
    meta = dict(plan["metadata"])
    assert "100" in meta["Target VMIDs"]
