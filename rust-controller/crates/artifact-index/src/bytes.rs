use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::{ArtifactError, DeclaredArtifact};

const EVIDENCE_LEVEL: &str = "supplied_bytes_matched";
const PUBLICATION_VERIFIED: bool = false;

/// Agreement of supplied slices with declarations, not their origin, validity,
/// publication, bootability, readiness, or native execution authority.
///
/// Reports cannot be deserialized:
/// ```compile_fail
/// use artifact_index::SuppliedByteMatch;
/// let _: SuppliedByteMatch = serde_json::from_str("{}").unwrap();
/// ```
///
/// Report construction is owned by the comparison function:
/// ```compile_fail
/// use artifact_index::SuppliedByteMatch;
/// let report = SuppliedByteMatch {
///     descriptor_fingerprint: String::new(),
///     iso_size_bytes: 1,
///     wim_size_bytes: 1,
/// };
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(into = "MatchContract")]
pub struct SuppliedByteMatch {
    descriptor_fingerprint: String,
    iso_size_bytes: u64,
    wim_size_bytes: u64,
}

impl SuppliedByteMatch {
    pub fn descriptor_fingerprint(&self) -> &str {
        &self.descriptor_fingerprint
    }
    pub fn iso_size_bytes(&self) -> u64 {
        self.iso_size_bytes
    }
    pub fn wim_size_bytes(&self) -> u64 {
        self.wim_size_bytes
    }
}

/// Hashes borrowed nonempty slices directly; retains no content or location.
pub fn compare_supplied_bytes(
    artifact: &DeclaredArtifact,
    iso: &[u8],
    wim: &[u8],
) -> Result<SuppliedByteMatch, ArtifactError> {
    if iso.is_empty() {
        return Err(ArtifactError::EmptyIso);
    }
    if wim.is_empty() {
        return Err(ArtifactError::EmptyWim);
    }
    if hex::encode(Sha256::digest(iso)) != artifact.iso_sha256() {
        return Err(ArtifactError::IsoMismatch);
    }
    if hex::encode(Sha256::digest(wim)) != artifact.wim_sha256() {
        return Err(ArtifactError::WimMismatch);
    }
    Ok(SuppliedByteMatch {
        descriptor_fingerprint: artifact.fingerprint()?,
        iso_size_bytes: iso.len() as u64,
        wim_size_bytes: wim.len() as u64,
    })
}

#[derive(Serialize)]
struct MatchContract {
    descriptor_fingerprint: String,
    iso_size_bytes: u64,
    wim_size_bytes: u64,
    evidence_level: &'static str,
    publication_verified: bool,
}

impl From<SuppliedByteMatch> for MatchContract {
    fn from(report: SuppliedByteMatch) -> Self {
        Self {
            descriptor_fingerprint: report.descriptor_fingerprint,
            iso_size_bytes: report.iso_size_bytes,
            wim_size_bytes: report.wim_size_bytes,
            evidence_level: EVIDENCE_LEVEL,
            publication_verified: PUBLICATION_VERIFIED,
        }
    }
}
