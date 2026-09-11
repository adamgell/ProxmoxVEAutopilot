//! Explicit synthetic power input for isolated fixture supervisors only.
//! Samples are independent of task receipts/configuration and never refreshed on read.
use super::{FixtureStageIdentity, SeedPower};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    path::Path,
};
use uuid::Uuid;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct TestPowerSample {
    pub version: u8,
    pub identity: FixtureStageIdentity,
    pub vmid: u32,
    pub daemon_generation: Uuid,
    pub observed_unix_ms: u64,
    pub lease_expires_unix_ms: u64,
    pub power: SeedPower,
}
fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid test power source")
}

/// Immutable, ordered samples for one prepared stop lease. This is explicit
/// synthetic supervisor input, not a production power adapter or DB capability.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct VersionedTestPowerSample {
    pub version: u8,
    pub sequence: u64,
    pub previous_sha256: Option<String>,
    pub stop: FixtureStageIdentity,
    pub authority: super::FixtureStopAuthorityV1,
    pub sample: TestPowerSample,
}

impl VersionedTestPowerSample {
    pub(crate) fn key(&self) -> io::Result<String> {
        Ok(format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&(
                &self.stop,
                &self.sample.identity,
                self.sample.daemon_generation,
            ))?)
        ))
    }
    fn validate(&self, generation: Uuid, now: u64) -> io::Result<()> {
        self.stop.validate()?;
        self.sample
            .validate(&self.sample.identity, generation, now)?;
        let a = &self.authority;
        if self.version != 2
            || !(1..=64).contains(&self.sequence)
            || self.stop.stage != super::FixtureLedgerStage::PeEnsureStopped
            || a.version != 1
            || a.grace_operation.is_nil()
            || a.decision_event.is_nil()
            || a.evidence_fence == 0
            || a.grace_due_unix_ms == 0
            || a.grace_due_unix_ms > a.decision_unix_ms
            || a.decision_unix_ms > a.lease_checked_unix_ms
            || self.sample.observed_unix_ms < a.lease_checked_unix_ms
            || self.sample.lease_expires_unix_ms != a.lease_expires_unix_ms
            || now >= a.original_deadline_unix_ms
            || self
                .previous_sha256
                .as_ref()
                .is_some_and(|s| s.len() != 64 || !s.bytes().all(|b| b.is_ascii_hexdigit()))
            || (self.sequence == 1) != self.previous_sha256.is_none()
        {
            return Err(invalid());
        }
        Ok(())
    }
    fn path(&self, directory: &Path, sequence: u64) -> io::Result<std::path::PathBuf> {
        Ok(directory.join(format!("test-power-v2-{}-{sequence:02}.json", self.key()?)))
    }
    fn chain(&self, directory: &Path) -> io::Result<()> {
        let mut previous: Option<Self> = None;
        let mut digest = None;
        for sequence in 1..self.sequence {
            let path = self.path(directory, sequence)?;
            let meta = fs::symlink_metadata(&path)?;
            if !meta.is_file() || meta.len() > 8192 {
                return Err(invalid());
            }
            let bytes = fs::read(path)?;
            let entry: Self = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
            entry.validate(self.sample.daemon_generation, entry.sample.observed_unix_ms)?;
            if entry.key()? != self.key()?
                || entry.sequence != sequence
                || entry.previous_sha256 != digest
                || entry.authority != self.authority
                || entry.sample.vmid != self.sample.vmid
                || previous
                    .as_ref()
                    .is_some_and(|p| p.sample.observed_unix_ms >= entry.sample.observed_unix_ms)
            {
                return Err(invalid());
            }
            digest = Some(format!("{:x}", Sha256::digest(&bytes)));
            previous = Some(entry);
        }
        if self.previous_sha256 != digest
            || previous.is_some_and(|p| p.sample.observed_unix_ms >= self.sample.observed_unix_ms)
        {
            return Err(invalid());
        }
        Ok(())
    }
    pub(crate) fn install(&self, directory: &Path, generation: Uuid, now: u64) -> io::Result<()> {
        self.validate(generation, now)?;
        self.chain(directory)?;
        // No historical replay may replace the selected tail.
        if self.sequence < 64 && self.path(directory, self.sequence + 1)?.exists() {
            return Err(invalid());
        }
        let bytes = serde_json::to_vec(self)?;
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(self.path(directory, self.sequence)?)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        fs::File::open(directory)?.sync_all()
    }
    pub(crate) fn read(&self, directory: &Path, generation: Uuid, now: u64) -> io::Result<()> {
        self.validate(generation, now)?;
        self.chain(directory)?;
        let path = self.path(directory, self.sequence)?;
        let meta = fs::symlink_metadata(&path)?;
        if !meta.is_file()
            || meta.len() > 8192
            || fs::read(path)? != serde_json::to_vec(self)?
            || (self.sequence < 64 && self.path(directory, self.sequence + 1)?.exists())
        {
            return Err(invalid());
        }
        Ok(())
    }
}
impl TestPowerSample {
    fn validate(
        &self,
        identity: &FixtureStageIdentity,
        generation: Uuid,
        now: u64,
    ) -> io::Result<()> {
        self.identity.validate()?;
        if self.version != 1
            || self.identity.stage != super::FixtureLedgerStage::StartPe
            || &self.identity != identity
            || self.vmid == 0
            || generation.is_nil()
            || self.daemon_generation != generation
            || self.observed_unix_ms == 0
            || self.observed_unix_ms > now
            || now - self.observed_unix_ms > 5000
            || now >= self.lease_expires_unix_ms
            || self.power.locked
            || !matches!(
                self.power.power,
                crate::PowerState::Running | crate::PowerState::Stopped
            )
        {
            return Err(invalid());
        }
        Ok(())
    }
    fn path(directory: &Path, identity: &FixtureStageIdentity) -> io::Result<std::path::PathBuf> {
        let digest = Sha256::digest(serde_json::to_vec(identity)?);
        Ok(directory.join(format!("test-power-{digest:x}.json")))
    }
    pub(crate) fn install(&self, directory: &Path, generation: Uuid, now: u64) -> io::Result<()> {
        self.validate(&self.identity, generation, now)?;
        let bytes = serde_json::to_vec(self)?;
        let path = Self::path(directory, &self.identity)?;
        match OpenOptions::new().create_new(true).write(true).open(&path) {
            Ok(mut file) => {
                file.write_all(&bytes)?;
                file.sync_all()?;
                fs::File::open(directory)?.sync_all()?;
                Ok(())
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                if fs::read(path)? == bytes {
                    // A prior process may have died after writing complete bytes
                    // but before syncing the file or directory entry.
                    fs::File::open(Self::path(directory, &self.identity)?)?.sync_all()?;
                    fs::File::open(directory)?.sync_all()?;
                    Ok(())
                } else {
                    Err(invalid())
                }
            }
            Err(error) => Err(error),
        }
    }
    pub(crate) fn read(
        directory: &Path,
        identity: &FixtureStageIdentity,
        generation: Uuid,
        now: u64,
    ) -> io::Result<Self> {
        let path = Self::path(directory, identity)?;
        let metadata = fs::symlink_metadata(&path)?;
        if !metadata.is_file() || metadata.len() > 4096 {
            return Err(invalid());
        }
        let sample: Self = serde_json::from_slice(&fs::read(path)?).map_err(|_| invalid())?;
        sample.validate(identity, generation, now)?;
        Ok(sample)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn versioned_samples_reject_replay_clock_rollback_conflict_and_torn_history() {
        let directory = std::env::temp_dir().join(format!("power-v2-{}", Uuid::now_v7()));
        fs::create_dir(&directory).unwrap();
        let generation = Uuid::now_v7();
        let identity = FixtureStageIdentity {
            operation: Uuid::now_v7(),
            stage: super::super::FixtureLedgerStage::StartPe,
            attempt: Uuid::now_v7(),
            generation: Uuid::now_v7(),
            owner: Uuid::now_v7(),
            request_sha256: "a".repeat(64),
        };
        let first = VersionedTestPowerSample {
            version: 2,
            sequence: 1,
            previous_sha256: None,
            stop: FixtureStageIdentity {
                stage: super::super::FixtureLedgerStage::PeEnsureStopped,
                ..identity.clone()
            },
            authority: super::super::FixtureStopAuthorityV1 {
                version: 1,
                grace_operation: Uuid::now_v7(),
                decision_event: Uuid::now_v7(),
                evidence_fence: 1,
                grace_due_unix_ms: 90,
                decision_unix_ms: 95,
                lease_checked_unix_ms: 99,
                lease_expires_unix_ms: 6000,
                original_deadline_unix_ms: 7000,
            },
            sample: TestPowerSample {
                version: 1,
                identity,
                vmid: 109,
                daemon_generation: generation,
                observed_unix_ms: 100,
                lease_expires_unix_ms: 6000,
                power: SeedPower {
                    power: crate::PowerState::Running,
                    locked: false,
                },
            },
        };
        first.install(&directory, generation, 100).unwrap();
        first.read(&directory, generation, 100).unwrap();
        assert!(first.install(&directory, generation, 100).is_err());
        assert!(first.read(&directory, generation, 99).is_err());
        assert!(first.read(&directory, generation, 5101).is_err());
        assert!(first.read(&directory, Uuid::now_v7(), 100).is_err());
        let mut next = first.clone();
        next.sequence = 2;
        next.previous_sha256 = Some(format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&first).unwrap())
        ));
        assert!(next.install(&directory, generation, 100).is_err());
        next.sample.observed_unix_ms = 101;
        let mut conflict = next.clone();
        conflict.authority.evidence_fence += 1;
        assert!(conflict.install(&directory, generation, 101).is_err());
        conflict = next.clone();
        conflict.sample.vmid += 1;
        assert!(conflict.install(&directory, generation, 101).is_err());
        conflict = next.clone();
        conflict.previous_sha256 = Some("b".repeat(64));
        assert!(conflict.install(&directory, generation, 101).is_err());
        next.install(&directory, generation, 101).unwrap();
        next.read(&directory, generation, 101).unwrap();
        assert!(first.read(&directory, generation, 101).is_err());
        fs::write(first.path(&directory, 1).unwrap(), b"{torn").unwrap();
        assert!(next.read(&directory, generation, 101).is_err());
        fs::remove_file(first.path(&directory, 1).unwrap()).unwrap();
        fs::remove_file(next.path(&directory, 2).unwrap()).unwrap();
        fs::remove_dir(directory).unwrap();
    }
    #[test]
    fn independent_sample_replay_expiry_and_restart() {
        let directory = std::env::temp_dir().join(format!("power-source-{}", Uuid::now_v7()));
        fs::create_dir(&directory).unwrap();
        let generation = Uuid::now_v7();
        let sample = TestPowerSample {
            version: 1,
            identity: FixtureStageIdentity {
                operation: Uuid::now_v7(),
                stage: crate::fixture_support::FixtureLedgerStage::StartPe,
                attempt: Uuid::now_v7(),
                generation: Uuid::now_v7(),
                owner: Uuid::now_v7(),
                request_sha256: "a".repeat(64),
            },
            vmid: 109,
            daemon_generation: generation,
            observed_unix_ms: 100,
            lease_expires_unix_ms: 6000,
            power: SeedPower {
                power: crate::PowerState::Running,
                locked: false,
            },
        };
        sample.install(&directory, generation, 100).unwrap();
        sample.install(&directory, generation, 101).unwrap();
        assert_eq!(
            TestPowerSample::read(&directory, &sample.identity, generation, 101)
                .unwrap()
                .observed_unix_ms,
            100
        );
        assert!(TestPowerSample::read(&directory, &sample.identity, generation, 5101).is_err());
        assert!(TestPowerSample::read(&directory, &sample.identity, Uuid::now_v7(), 101).is_err());
        let mut other_owner = sample.identity.clone();
        other_owner.owner = Uuid::now_v7();
        assert!(TestPowerSample::read(&directory, &other_owner, generation, 101).is_err());
        let mut expired = sample.clone();
        expired.lease_expires_unix_ms = 101;
        assert!(
            expired
                .validate(&expired.identity, generation, 101)
                .is_err()
        );
        let mut conflict = sample.clone();
        conflict.power.power = crate::PowerState::Stopped;
        assert!(conflict.install(&directory, generation, 101).is_err());
        // A separate explicitly seeded Stopped sample is observable without a
        // successful task receipt or any VM configuration in the source.
        conflict.identity.operation = Uuid::now_v7();
        conflict.install(&directory, generation, 101).unwrap();
        assert_eq!(
            TestPowerSample::read(&directory, &conflict.identity, generation, 101)
                .unwrap()
                .power
                .power,
            crate::PowerState::Stopped
        );
        let stopped_path = TestPowerSample::path(&directory, &conflict.identity).unwrap();
        fs::remove_file(stopped_path).unwrap();
        let path = TestPowerSample::path(&directory, &sample.identity).unwrap();
        fs::write(&path, b"{torn").unwrap();
        assert!(TestPowerSample::read(&directory, &sample.identity, generation, 101).is_err());
        fs::remove_file(path).unwrap();
        fs::remove_dir(directory).unwrap();
    }
}
