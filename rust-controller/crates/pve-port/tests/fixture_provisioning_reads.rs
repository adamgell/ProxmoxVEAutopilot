#![cfg(feature = "fixture-ipc")]
mod provisioning_seed_support;
use pve_port::fixture_support::FixtureProvisioningReads;
use serde_json::json;

#[test]
fn target_presence_and_absence_keep_independent_power_observations() {
    use pve_port::{
        PowerState,
        fixture_support::{SeedConfig, SeedRead, SeedReadError},
    };
    for seed in [
        provisioning_seed_support::target_present(),
        provisioning_seed_support::target_absent(),
    ] {
        let decoded =
            FixtureProvisioningReads::decode(&serde_json::to_vec(&seed).unwrap(), &seed.identity)
                .unwrap();
        assert_eq!(decoded, seed);
        assert!(matches!(
            decoded.source_power,
            SeedRead::Observed {
                value: PowerState::Stopped,
                ..
            }
        ));
        match decoded.target_config {
            SeedRead::Observed {
                value: SeedConfig::Present { config },
                ..
            } => {
                assert_eq!(config.vmid().get(), 101);
                assert!(matches!(
                    decoded.target_power,
                    SeedRead::Observed {
                        value: PowerState::Running,
                        ..
                    }
                ));
            }
            SeedRead::Observed {
                value: SeedConfig::Absent {},
                ..
            } => {
                assert!(matches!(
                    decoded.target_power,
                    SeedRead::Error {
                        error: SeedReadError::Unavailable,
                        ..
                    }
                ));
            }
            _ => panic!("lost explicit target presence observation"),
        }
    }
}

#[test]
fn target_config_identity_time_and_power_envelopes_are_strict() {
    let seed = provisioning_seed_support::target_present();
    let value = serde_json::to_value(&seed).unwrap();
    for (pointer, replacement) in [
        ("/target_config/value/config/node", json!("other-node")),
        ("/target_config/value/config/vmid", json!(900)),
        ("/target_config/observed_unix_ms", json!(123)),
        ("/source_power/observed_unix_ms", json!(0)),
        ("/target_power/value", json!("unknown")),
    ] {
        let mut invalid = value.clone();
        *invalid.pointer_mut(pointer).unwrap() = replacement;
        assert!(
            FixtureProvisioningReads::decode(
                &serde_json::to_vec(&invalid).unwrap(),
                &seed.identity
            )
            .is_err(),
            "accepted {pointer}"
        );
    }
    for family in ["target_config", "source_power", "target_power"] {
        let mut invalid = value.clone();
        invalid[family]["unexpected"] = json!(true);
        assert!(
            FixtureProvisioningReads::decode(
                &serde_json::to_vec(&invalid).unwrap(),
                &seed.identity
            )
            .is_err()
        );
    }
    let mut absent = serde_json::to_value(provisioning_seed_support::target_absent()).unwrap();
    absent["target_config"]["value"]["config"] = value["target_config"]["value"]["config"].clone();
    assert!(
        FixtureProvisioningReads::decode(&serde_json::to_vec(&absent).unwrap(), &seed.identity)
            .is_err()
    );
    let bytes = serde_json::to_string(&seed).unwrap();
    let duplicate = bytes.replacen(
        "\"target_power\":",
        &format!(
            "\"target_power\":{},\"target_power\":",
            value["target_power"]
        ),
        1,
    );
    assert!(FixtureProvisioningReads::decode(duplicate.as_bytes(), &seed.identity).is_err());
}

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
