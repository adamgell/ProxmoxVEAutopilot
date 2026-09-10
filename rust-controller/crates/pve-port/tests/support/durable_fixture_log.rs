//! Local fixture substrate only. The supervisor must own this file exclusively.
//! No controller or production transport imports this module.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs::{File, OpenOptions},
    io::{self, BufRead, BufReader, Read, Write},
    path::Path,
};
use uuid::Uuid;

const MAX_LINE: usize = 4096;

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Attempt {
    version: u8,
    sequence: u64,
    operation: Uuid,
    request_sha256: String,
    duplicate: bool,
}

pub struct FixtureLog {
    file: File,
    records: Vec<Attempt>,
    attempted: BTreeSet<Uuid>,
    poisoned: bool,
}

fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid fixture log")
}

fn frame(record: &Attempt) -> io::Result<Vec<u8>> {
    let payload = serde_json::to_vec(record).map_err(|_| invalid())?;
    Ok(format!(
        "{:08x}:{}:{:x}\n",
        payload.len(),
        std::str::from_utf8(&payload).map_err(|_| invalid())?,
        Sha256::digest(&payload)
    )
    .into_bytes())
}

impl FixtureLog {
    /// Creates a fresh file, never overwriting an existing fixture.
    pub fn create(path: &Path) -> io::Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .append(true)
            .create_new(true)
            .open(path)?;
        file.sync_all()?;
        // Persist the new directory entry before acknowledging creation.
        File::open(
            path.parent()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or(Path::new(".")),
        )?
        .sync_all()?;
        Ok(Self {
            file,
            records: Vec::new(),
            attempted: BTreeSet::new(),
            poisoned: false,
        })
    }

    pub fn recover(path: &Path) -> io::Result<Self> {
        let file = OpenOptions::new().read(true).append(true).open(path)?;
        let mut reader = BufReader::new(file.try_clone()?);
        let mut records = Vec::new();
        let mut attempted = BTreeSet::new();
        loop {
            let mut line = Vec::new();
            let n = reader
                .by_ref()
                .take((MAX_LINE + 1) as u64)
                .read_until(b'\n', &mut line)?;
            if n == 0 {
                break;
            }
            if n > MAX_LINE || line.last() != Some(&b'\n') || n < 75 || line[8] != b':' {
                return Err(invalid());
            }
            let length =
                usize::from_str_radix(std::str::from_utf8(&line[..8]).map_err(|_| invalid())?, 16)
                    .map_err(|_| invalid())?;
            if length.checked_add(75) != Some(n) {
                return Err(invalid());
            }
            let record: Attempt =
                serde_json::from_slice(&line[9..9 + length]).map_err(|_| invalid())?;
            // Exact regeneration checks checksum, framing and canonical JSON together.
            if frame(&record)? != line
                || record.version != 1
                || record.sequence != records.len() as u64 + 1
                || record.operation.is_nil()
                || !valid_digest(&record.request_sha256)
                || record.duplicate != attempted.contains(&record.operation)
            {
                return Err(invalid());
            }
            attempted.insert(record.operation);
            records.push(record);
        }
        Ok(Self {
            file,
            records,
            attempted,
            poisoned: false,
        })
    }

    /// Returns true for a duplicate, *after* durably recording that attempt.
    /// This is admission bookkeeping, never an accepted-effect acknowledgement.
    pub fn record_attempt(&mut self, operation: Uuid, digest: &str) -> io::Result<bool> {
        if self.poisoned {
            return Err(io::Error::other("fixture log requires recovery"));
        }
        if operation.is_nil() || !valid_digest(digest) {
            return Err(invalid());
        }
        let duplicate = self.attempted.contains(&operation);
        let record = Attempt {
            version: 1,
            sequence: u64::try_from(self.records.len())
                .ok()
                .and_then(|n| n.checked_add(1))
                .ok_or_else(invalid)?,
            operation,
            request_sha256: digest.into(),
            duplicate,
        };
        let bytes = frame(&record)?;
        self.poisoned = true;
        self.file.write_all(&bytes)?;
        // sync_data is Rust's fdatasync-equivalent; no success is returned before it.
        self.file.sync_data()?;
        self.attempted.insert(operation);
        self.records.push(record);
        self.poisoned = false;
        Ok(duplicate)
    }

    pub fn records(&self) -> &[Attempt] {
        &self.records
    }
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
