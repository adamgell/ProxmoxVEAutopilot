"""Tests for /vms hostname bubble evidence helpers."""
import json


def _row(**probe_overrides):
    probe = {
        "ad_found": 0,
        "ad_match_count": 0,
        "ad_matches_json": "[]",
        "entra_found": 0,
        "entra_match_count": 0,
        "entra_matches_json": "[]",
        "intune_found": 0,
        "intune_match_count": 0,
        "intune_matches_json": "[]",
        "dsreg_status": "{}",
    }
    probe.update(probe_overrides)
    return {"vmid": 108, "probe": probe}


def test_hostname_join_evidence_prefers_direct_ad_domain():
    from web.monitoring_evidence import hostname_join_evidence

    evidence = hostname_join_evidence(_row(
        ad_found=1,
        ad_match_count=1,
        ad_matches_json=json.dumps([{"domain": "home.gell.com"}]),
        entra_found=1,
        entra_match_count=1,
        entra_matches_json=json.dumps([{"trustType": "ServerAd"}]),
    ))

    assert evidence["label"] == "domain"
    assert evidence["title"] == "Active Directory domain-joined"
    assert evidence["source"] == "ad"
    assert evidence["priority"] > 0
    assert evidence["is_joined"] is True


def test_hostname_join_evidence_uses_hybrid_serverad_as_domain():
    from web.monitoring_evidence import hostname_join_evidence

    evidence = hostname_join_evidence(_row(
        entra_found=1,
        entra_match_count=1,
        entra_matches_json=json.dumps([{"trustType": "ServerAd"}]),
    ))

    assert evidence == {
        "label": "domain",
        "title": "Hybrid Entra join reported by Entra trustType=ServerAd",
        "source": "entra_trust_serverad",
        "priority": 90,
        "is_joined": True,
    }


def test_hostname_join_evidence_uses_cloud_azuread_as_entra_id():
    from web.monitoring_evidence import hostname_join_evidence

    evidence = hostname_join_evidence(_row(
        entra_found=1,
        entra_match_count=1,
        entra_matches_json=json.dumps([{"trustType": "AzureAd"}]),
    ))

    assert evidence == {
        "label": "Entra ID",
        "title": "Entra ID joined",
        "source": "entra_trust_azuread",
        "priority": 80,
        "is_joined": True,
    }


def test_hostname_join_evidence_uses_dsreg_azure_ad_joined_as_entra_id():
    from web.monitoring_evidence import hostname_join_evidence

    evidence = hostname_join_evidence(_row(
        dsreg_status=json.dumps({"AzureAdJoined": "YES", "TenantName": "home"}),
    ))

    assert evidence == {
        "label": "Entra ID",
        "title": "Entra ID joined from dsreg AzureAdJoined",
        "source": "dsreg_azure_ad_joined",
        "priority": 70,
        "is_joined": True,
    }


def test_hostname_join_evidence_surfaces_intune_to_entra_device_id_intersection():
    from web.monitoring_evidence import hostname_join_evidence

    device_id = "6a0ba1f9-0090-4683-aee3-31a6abc1e4ad"
    evidence = hostname_join_evidence(_row(
        entra_found=1,
        entra_match_count=1,
        entra_matches_json=json.dumps([{
            "displayName": "WIN-C4P3CQ6R5LQ",
            "deviceId": device_id,
            "trustType": "AzureAd",
        }]),
        intune_found=1,
        intune_match_count=1,
        intune_matches_json=json.dumps([{
            "deviceName": "WIN-C4P3CQ6R5LQ",
            "azureADDeviceId": device_id,
        }]),
    ))

    assert evidence == {
        "label": "Entra ID",
        "title": "Entra ID joined via Intune azureADDeviceId -> Entra deviceId",
        "source": "intune_entra_device_id",
        "priority": 85,
        "is_joined": True,
    }


def test_hostname_join_evidence_returns_workgroup_only_without_join_evidence():
    from web.monitoring_evidence import hostname_join_evidence

    evidence = hostname_join_evidence(_row(
        entra_found=1,
        entra_match_count=1,
        entra_matches_json=json.dumps([{"trustType": "Workplace"}]),
        intune_found=1,
        intune_match_count=1,
        intune_matches_json=json.dumps([{"azureADDeviceId": "intune-only"}]),
    ))

    assert evidence == {
        "label": "workgroup",
        "title": "Not joined to an AD domain",
        "source": "none",
        "priority": 0,
        "is_joined": False,
    }
