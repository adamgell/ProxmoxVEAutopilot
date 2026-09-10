#![cfg(feature = "fixture-ipc")]

mod provisioning_support;

use provisioning_support::{chain, time};
use pve_port::*;

/// These are the reads made by operation-controller's Clone collector. A daemon
/// adapter must supply them from its own world before it can expose dispatch.
#[test]
fn clone_admission_requires_each_daemon_read_family() {
    let episode = chain().remove(0);
    assert_eq!(
        evaluate_provisioning_preflight(&episode.pre, &episode.pre_evidence, time()).decision,
        NativeDecision::Ready
    );
    for missing in [
        "node",
        "storage",
        "bridges",
        "inventory",
        "identity",
        "source_config",
        "source_power",
        "target_config",
        "media",
        "coverage",
    ] {
        let mut evidence = episode.pre_evidence.facts().clone();
        match missing {
            "node" => evidence.node = None,
            "storage" => evidence.storage = None,
            "bridges" => evidence.bridges = None,
            "inventory" => evidence.inventory = None,
            "identity" => evidence.identities.clear(),
            "source_config" => evidence.source_config = None,
            "source_power" => evidence.source_power = None,
            "target_config" => evidence.target_config = None,
            "media" => evidence.media.clear(),
            "coverage" => evidence.inventory_coverage = ProvisioningCoverageV1::Partial,
            _ => unreachable!(),
        }
        // Structural rejection is also a closed admission gate. Valid evidence
        // with an unavailable observation must never acquire Ready authority.
        if let Ok(evidence) = ProvisioningEvidenceV1::new(evidence) {
            let result = evaluate_provisioning_preflight(&episode.pre, &evidence, time());
            assert_ne!(result.decision, NativeDecision::Ready, "missing {missing}");
        }
    }
}

#[test]
fn clone_vacancy_does_not_require_power_for_an_absent_target() {
    let episode = chain().remove(0);
    let mut evidence = episode.pre_evidence.facts().clone();
    evidence.target_power = None;
    let evidence = ProvisioningEvidenceV1::new(evidence).unwrap();
    assert_eq!(
        evaluate_provisioning_preflight(&episode.pre, &evidence, time()).decision,
        NativeDecision::Ready
    );
}

#[test]
fn node_inventory_cannot_be_promoted_to_global_identity_coverage() {
    let episode = chain().remove(0);
    let mut evidence = episode.pre_evidence.facts().clone();
    evidence.inventory_coverage = ProvisioningCoverageV1::Partial;
    let evidence = ProvisioningEvidenceV1::new(evidence).unwrap();
    assert_ne!(
        evaluate_provisioning_preflight(&episode.pre, &evidence, time()).decision,
        NativeDecision::Ready
    );
}
