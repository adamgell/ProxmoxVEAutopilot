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
