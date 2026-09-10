#![cfg(feature = "fixture-ipc")]
mod provisioning_seed_support;
use pve_port::fixture_support::FixtureProvisioningReads;
use serde_json::json;

#[test]
fn populated_config_media_and_explicit_errors_round_trip_without_refresh() {
    let seed = provisioning_seed_support::populated();
    let bytes = serde_json::to_vec(&seed).unwrap();
    assert_eq!(
        FixtureProvisioningReads::decode(&bytes, &seed.identity).unwrap(),
        seed
    );
}

#[test]
fn populated_config_and_media_reject_wrong_identity_time_and_invalid_content() {
    let seed = provisioning_seed_support::populated();
    let value = serde_json::to_value(&seed).unwrap();
    for (pointer, replacement) in [
        ("/source_config/value/config/node", json!("other-node")),
        ("/source_config/value/config/vmid", json!(101)),
        ("/source_config/observed_unix_ms", json!(123)),
        ("/deployment_media/value/node", json!("other-node")),
        ("/deployment_media/observed_unix_ms", json!(123)),
        (
            "/driver_media/value/iso_volids",
            json!(["local:iso/a.iso", "local:iso/a.iso"]),
        ),
        ("/driver_media/value/iso_volids", json!(["other:iso/a.iso"])),
        ("/target_power/observed_unix_ms", json!(0)),
    ] {
        let mut invalid = value.clone();
        *invalid
            .pointer_mut(pointer)
            .unwrap_or_else(|| panic!("missing {pointer}")) = replacement;
        assert!(
            FixtureProvisioningReads::decode(
                &serde_json::to_vec(&invalid).unwrap(),
                &seed.identity
            )
            .is_err(),
            "accepted {pointer}"
        );
    }
}
