#[path = "support/durable_fixture_log.rs"]
mod durable_fixture_log;

use durable_fixture_log::FixtureLog;
use std::{fs, io::Write, path::PathBuf};
use uuid::Uuid;

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
