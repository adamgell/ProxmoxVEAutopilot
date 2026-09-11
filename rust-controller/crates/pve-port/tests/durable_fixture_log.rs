#[path = "../src/fixture_support/durable_fixture_log.rs"]
mod durable_fixture_log;

use durable_fixture_log::FixtureLog;
use std::{fs, io::Write, path::PathBuf};
use uuid::Uuid;

use durable_fixture_log::VmState;

fn framed(value: &impl serde::Serialize) -> Vec<u8> {
    use sha2::{Digest, Sha256};
    let payload = serde_json::to_vec(value).unwrap();
    format!(
        "{:08x}:{}:{:x}\n",
        payload.len(),
        std::str::from_utf8(&payload).unwrap(),
        Sha256::digest(&payload)
    )
    .into_bytes()
}

#[test]
fn stage_replay_rejects_canonical_but_rebound_effects_and_ambiguous_legacy() {
    use durable_fixture_log::{Attempt, Effect, FixtureLedgerStage, StageBinding};
    let fixture = Fixture::new();
    let operation = Uuid::now_v7();
    let digest = "a".repeat(64);
    let binding = StageBinding {
        stage: FixtureLedgerStage::Clone,
        attempt: Uuid::now_v7(),
        generation: Uuid::now_v7(),
        owner: Uuid::now_v7(),
    };
    let mut log = FixtureLog::create(&fixture.path()).unwrap();
    log.record_stage_attempt(operation, &digest, binding)
        .unwrap();
    log.record_effect(
        1,
        100,
        None,
        VmState {
            disk_bytes: 80,
            pe_configured: false,
        },
    )
    .unwrap();
    let attempt = framed(&log.records()[0]);
    let original = serde_json::to_value(&log.effects()[0]).unwrap();
    let original_attempt = serde_json::to_value(&log.records()[0]).unwrap();
    drop(log);
    for (field, value) in [
        ("stage", serde_json::json!("disk_capacity")),
        ("attempt", serde_json::json!(Uuid::now_v7())),
        ("generation", serde_json::json!(Uuid::now_v7())),
        ("owner", serde_json::json!(Uuid::now_v7())),
    ] {
        let mut changed = original.clone();
        changed["stage_binding"][field] = value;
        let effect: Effect = serde_json::from_value(changed).unwrap();
        fs::write(fixture.path(), [attempt.clone(), framed(&effect)].concat()).unwrap();
        assert!(
            FixtureLog::recover(&fixture.path()).is_err(),
            "accepted rebound {field}"
        );
    }
    let mut legacy = original_attempt;
    legacy["version"] = serde_json::json!(1);
    legacy["sequence"] = serde_json::json!(2);
    legacy.as_object_mut().unwrap().remove("stage_binding");
    let legacy: Attempt = serde_json::from_value(legacy).unwrap();
    fs::write(fixture.path(), [attempt, framed(&legacy)].concat()).unwrap();
    assert!(FixtureLog::recover(&fixture.path()).is_err());
}

#[test]
fn stage_keys_replay_exactly_and_bind_original_authority() {
    use durable_fixture_log::{FixtureLedgerStage, StageBinding};
    let fixture = Fixture::new();
    let operation = Uuid::now_v7();
    let generation = Uuid::now_v7();
    let owner = Uuid::now_v7();
    let bindings = [
        FixtureLedgerStage::Clone,
        FixtureLedgerStage::DiskCapacity,
        FixtureLedgerStage::ConfigurePe,
    ]
    .map(|stage| StageBinding {
        stage,
        attempt: Uuid::now_v7(),
        generation,
        owner,
    });
    let mut log = FixtureLog::create(&fixture.path()).unwrap();
    for (index, binding) in bindings.iter().enumerate() {
        let digest = format!("{:064x}", index + 1);
        assert!(
            !log.record_stage_attempt(operation, &digest, *binding)
                .unwrap()
        );
        // This tests the ledger transition contract only, not PVE stage behavior.
        log.record_effect_receipt(
            index as u64 + 1,
            index as u32 + 100,
            None,
            VmState {
                disk_bytes: 80,
                pe_configured: false,
            },
            Some(vec![index as u8]),
        )
        .unwrap();
    }
    let attempts = serde_json::to_value(log.records()).unwrap();
    let effects = log.effects().to_vec();
    drop(log);
    let mut log = FixtureLog::recover(&fixture.path()).unwrap();
    assert_eq!(serde_json::to_value(log.records()).unwrap(), attempts);
    assert_eq!(log.effects(), effects);
    for (index, binding) in bindings.iter().enumerate() {
        let digest = format!("{:064x}", index + 1);
        assert_eq!(
            log.accepted_stage_effect(operation, &digest, *binding)
                .unwrap(),
            Some(&effects[index])
        );
        for wrong in [
            StageBinding {
                attempt: Uuid::now_v7(),
                ..*binding
            },
            StageBinding {
                generation: Uuid::now_v7(),
                ..*binding
            },
            StageBinding {
                owner: Uuid::now_v7(),
                ..*binding
            },
            StageBinding {
                stage: bindings[(index + 1) % 3].stage,
                ..*binding
            },
        ] {
            assert!(
                log.accepted_stage_effect(operation, &digest, wrong)
                    .is_err()
            );
            assert!(log.record_stage_attempt(operation, &digest, wrong).unwrap());
            assert!(
                log.record_effect(
                    log.records().len() as u64,
                    500,
                    None,
                    VmState {
                        disk_bytes: 80,
                        pe_configured: false
                    }
                )
                .is_err()
            );
        }
        assert!(
            log.accepted_stage_effect(operation, &"f".repeat(64), *binding)
                .is_err()
        );
    }
    assert_eq!(log.effects(), effects);
    drop(log);
    assert_eq!(
        FixtureLog::recover(&fixture.path()).unwrap().effects(),
        effects
    );
}

#[test]
fn stage_identity_rejects_legacy_mixing_and_invalid_authority() {
    use durable_fixture_log::{FixtureLedgerStage, StageBinding};
    for legacy_first in [true, false] {
        let fixture = Fixture::new();
        let mut log = FixtureLog::create(&fixture.path()).unwrap();
        let operation = Uuid::now_v7();
        let digest = "a".repeat(64);
        let binding = StageBinding {
            stage: FixtureLedgerStage::Clone,
            attempt: Uuid::now_v7(),
            generation: Uuid::now_v7(),
            owner: Uuid::now_v7(),
        };
        for bad in [
            StageBinding {
                attempt: Uuid::nil(),
                ..binding
            },
            StageBinding {
                generation: Uuid::nil(),
                ..binding
            },
            StageBinding {
                owner: Uuid::nil(),
                ..binding
            },
        ] {
            assert!(log.record_stage_attempt(operation, &digest, bad).is_err());
        }
        if legacy_first {
            log.record_attempt(operation, &digest).unwrap();
            assert!(
                log.record_stage_attempt(operation, &digest, binding)
                    .is_err()
            );
            assert!(
                log.accepted_stage_effect(operation, &digest, binding)
                    .is_err()
            );
        } else {
            log.record_stage_attempt(operation, &digest, binding)
                .unwrap();
            assert!(log.record_attempt(operation, &digest).is_err());
            assert!(log.accepted_effect(operation, &digest).is_err());
        }
        assert_eq!(log.records().len(), 1);
        drop(log);
        assert_eq!(
            FixtureLog::recover(&fixture.path())
                .unwrap()
                .records()
                .len(),
            1
        );
    }
}

#[test]
fn effect_lookup_rejects_invalid_identity_without_appending() {
    let fixture = Fixture::new();
    let log = FixtureLog::create(&fixture.path()).unwrap();
    assert!(log.accepted_effect(Uuid::nil(), &"a".repeat(64)).is_err());
    assert!(log.accepted_effect(Uuid::now_v7(), "invalid").is_err());
    assert!(
        log.accepted_effect(Uuid::now_v7(), &"a".repeat(64))
            .unwrap()
            .is_none()
    );
    assert!(log.records().is_empty());
    assert!(log.effects().is_empty());
    assert!(fs::read(fixture.path()).unwrap().is_empty());
}

#[test]
fn acceptance_and_world_transition_recover_together_after_durable_attempt() {
    let fixture = Fixture::new();
    let mut log = FixtureLog::create(&fixture.path()).unwrap();
    let initial = VmState {
        disk_bytes: 80,
        pe_configured: false,
    };
    assert!(log.record_effect(1, 100, None, initial.clone()).is_err());
    let operation = Uuid::now_v7();
    log.record_attempt(operation, &"a".repeat(64)).unwrap();
    drop(log);
    let mut log = FixtureLog::recover(&fixture.path()).unwrap();
    assert!(log.effects().is_empty());
    assert!(log.world().is_empty());
    log.record_effect(1, 100, None, initial.clone()).unwrap();
    assert!(log.effects()[0].matches(operation, &"a".repeat(64)));
    assert!(!log.effects()[0].matches(Uuid::now_v7(), &"a".repeat(64)));
    assert!(!log.effects()[0].matches(operation, &"b".repeat(64)));
    assert!(
        log.record_effect(1, 100, Some(initial.clone()), initial.clone())
            .is_err()
    );
    log.record_attempt(Uuid::now_v7(), &"b".repeat(64)).unwrap();
    let configured = VmState {
        disk_bytes: 120,
        pe_configured: true,
    };
    assert!(log.record_effect(2, 100, None, configured.clone()).is_err());
    log.record_effect(2, 100, Some(initial), configured.clone())
        .unwrap();
    drop(log);
    let log = FixtureLog::recover(&fixture.path()).unwrap();
    assert_eq!(log.records().len(), 2);
    assert_eq!(log.effects().len(), 2);
    assert_eq!(log.world().get(&100), Some(&configured));
}

#[test]
fn duplicate_attempt_cannot_accept_and_reordered_valid_frames_fail_recovery() {
    let fixture = Fixture::new();
    let operation = Uuid::now_v7();
    let mut log = FixtureLog::create(&fixture.path()).unwrap();
    log.record_attempt(operation, &"a".repeat(64)).unwrap();
    log.record_attempt(operation, &"a".repeat(64)).unwrap();
    let state = VmState {
        disk_bytes: 80,
        pe_configured: false,
    };
    assert!(log.record_effect(2, 100, None, state.clone()).is_err());
    log.record_effect(1, 100, None, state).unwrap();
    drop(log);
    let bytes = fs::read(fixture.path()).unwrap();
    let lines: Vec<_> = bytes.split_inclusive(|b| *b == b'\n').collect();
    // Individually canonical/checksummed frames cannot be replayed out of order.
    fs::write(fixture.path(), [lines[2], lines[0], lines[1]].concat()).unwrap();
    assert!(FixtureLog::recover(&fixture.path()).is_err());
    // A valid attempt prefix with a partial accepted transition fails closed.
    fs::write(fixture.path(), &bytes[..bytes.len() - 1]).unwrap();
    assert!(FixtureLog::recover(&fixture.path()).is_err());
}

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("pve-fixture-log-{}", Uuid::now_v7()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn path(&self) -> PathBuf {
        self.0.join("fixture.log")
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

#[test]
fn restart_preserves_every_duplicate_attempt_including_changed_digest() {
    let fixture = Fixture::new();
    let operation = Uuid::now_v7();
    let mut log = FixtureLog::create(&fixture.path()).unwrap();
    assert!(!log.record_attempt(operation, &"a".repeat(64)).unwrap());
    assert!(log.record_attempt(operation, &"a".repeat(64)).unwrap());
    drop(log);
    let mut recovered = FixtureLog::recover(&fixture.path()).unwrap();
    assert_eq!(recovered.records().len(), 2);
    assert!(
        recovered
            .record_attempt(operation, &"b".repeat(64))
            .unwrap()
    );
    assert!(
        !recovered
            .record_attempt(Uuid::now_v7(), &"c".repeat(64))
            .unwrap()
    );
    drop(recovered);
    assert_eq!(
        FixtureLog::recover(&fixture.path())
            .unwrap()
            .records()
            .len(),
        4
    );
}

#[test]
fn every_truncated_record_and_checksum_corruption_fail_closed() {
    let fixture = Fixture::new();
    let mut log = FixtureLog::create(&fixture.path()).unwrap();
    log.record_attempt(Uuid::now_v7(), &"a".repeat(64)).unwrap();
    drop(log);
    let original = fs::read(fixture.path()).unwrap();
    for end in 1..original.len() {
        fs::write(fixture.path(), &original[..end]).unwrap();
        assert!(
            FixtureLog::recover(&fixture.path()).is_err(),
            "accepted prefix {end}"
        );
        assert_eq!(fs::read(fixture.path()).unwrap(), original[..end]);
    }
    let mut corrupt = original;
    let index = corrupt.len() - 2;
    corrupt[index] = if corrupt[index] == b'0' { b'1' } else { b'0' };
    fs::write(fixture.path(), corrupt).unwrap();
    assert!(FixtureLog::recover(&fixture.path()).is_err());
}

#[test]
fn rejects_trailing_garbage_oversized_lines_and_replayed_frames() {
    let fixture = Fixture::new();
    let mut log = FixtureLog::create(&fixture.path()).unwrap();
    log.record_attempt(Uuid::now_v7(), &"a".repeat(64)).unwrap();
    drop(log);
    let original = fs::read(fixture.path()).unwrap();
    for suffix in [b"partial".to_vec(), vec![b'x'; 4097], original.clone()] {
        fs::write(fixture.path(), &original).unwrap();
        fs::OpenOptions::new()
            .append(true)
            .open(fixture.path())
            .unwrap()
            .write_all(&suffix)
            .unwrap();
        assert!(FixtureLog::recover(&fixture.path()).is_err());
    }
}

#[test]
fn invalid_inputs_do_not_append_and_create_never_overwrites() {
    let fixture = Fixture::new();
    let mut log = FixtureLog::create(&fixture.path()).unwrap();
    assert!(log.record_attempt(Uuid::nil(), &"a".repeat(64)).is_err());
    assert!(log.record_attempt(Uuid::now_v7(), &"A".repeat(64)).is_err());
    assert!(FixtureLog::create(&fixture.path()).is_err());
    drop(log);
    assert!(
        FixtureLog::recover(&fixture.path())
            .unwrap()
            .records()
            .is_empty()
    );
    assert_eq!(fs::metadata(fixture.path()).unwrap().len(), 0);
}
