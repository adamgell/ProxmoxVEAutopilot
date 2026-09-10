#[path = "../provisioning_support/mod.rs"]
mod support;
use pve_port::{fixture_support::*, *};
use uuid::Uuid;

pub fn populated() -> FixtureProvisioningReads {
    let observed_unix_ms = support::time().timestamp_millis() as u64;
    let media = ProvisioningMediaInventoryV1::new(
        support::node(),
        StorageName::parse("local").unwrap(),
        vec!["local:iso/deployment.iso".into()],
        ProvisioningCoverageV1::Complete,
        support::time(),
    )
    .unwrap();
    FixtureProvisioningReads {
        version: 1,
        identity: FixtureProvisioningIdentity {
            fixture_id: Uuid::from_u128(1),
            operation: Uuid::from_u128(2),
            request_sha256: "a".repeat(64),
            node: "pve-test".into(),
            source_vmid: 900,
            target_vmid: 101,
        },
        source_config: SeedRead::Observed {
            observed_unix_ms,
            value: SeedConfig::Present {
                config: Box::new(support::source()),
            },
        },
        target_config: SeedRead::Error {
            observed_unix_ms: observed_unix_ms + 1,
            error: SeedReadError::Unavailable,
        },
        source_power: SeedRead::Observed {
            observed_unix_ms: observed_unix_ms + 2,
            value: PowerState::Stopped,
        },
        target_power: SeedRead::Error {
            observed_unix_ms: observed_unix_ms + 3,
            error: SeedReadError::Forbidden,
        },
        deployment_media: SeedRead::Observed {
            observed_unix_ms,
            value: media.clone(),
        },
        driver_media: SeedRead::Observed {
            observed_unix_ms,
            value: media,
        },
    }
}

pub fn target_present() -> FixtureProvisioningReads {
    let mut seed = populated();
    seed.target_config = SeedRead::Observed {
        observed_unix_ms: support::time().timestamp_millis() as u64,
        value: SeedConfig::Present {
            config: Box::new(support::intermediate()),
        },
    };
    seed.target_power = SeedRead::Observed {
        observed_unix_ms: support::time().timestamp_millis() as u64 + 4,
        value: PowerState::Running,
    };
    seed
}

pub fn target_absent() -> FixtureProvisioningReads {
    let mut seed = populated();
    seed.target_config = SeedRead::Observed {
        observed_unix_ms: support::time().timestamp_millis() as u64 + 5,
        value: SeedConfig::Absent {},
    };
    seed.target_power = SeedRead::Error {
        observed_unix_ms: support::time().timestamp_millis() as u64 + 6,
        error: SeedReadError::Unavailable,
    };
    seed
}
