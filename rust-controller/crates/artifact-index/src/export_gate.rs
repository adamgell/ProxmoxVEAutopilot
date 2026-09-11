//! Supplied-byte export integrity and rollback-window diagnostics only.
use serde::Serialize;
use sha2::{Digest, Sha256};

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ExportIntegrityV1 {
    version: u8,
    source_sha256: String,
    image_sha256: String,
    proof_sha256: String,
    rollback_image_sha256: String,
    rollback_proof_sha256: String,
    rollback_opened_unix_micros: u64,
    rollback_deadline_unix_micros: u64,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum ExportGateError {
    #[error("invalid export integrity declaration")]
    InvalidDeclaration,
    #[error("export integrity identity mismatch")]
    IdentityMismatch,
    #[error("supplied export bytes differ from declaration")]
    DigestMismatch,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExportGateRefusal {
    RollbackWindowClosed,
    CutoverAndRollbackAuthorityUnavailable,
}
impl ExportIntegrityV1 {
    /// Hash order: exact source export, image export, proof bundle, rollback
    /// image export, rollback proof bundle. No content semantics are inferred.
    pub fn new(hashes: [String; 5], opened: u64, deadline: u64) -> Result<Self, ExportGateError> {
        if opened == 0
            || deadline <= opened
            || hashes.iter().any(|h| {
                h.len() != 64
                    || !h
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            })
        {
            return Err(ExportGateError::InvalidDeclaration);
        }
        let [
            source_sha256,
            image_sha256,
            proof_sha256,
            rollback_image_sha256,
            rollback_proof_sha256,
        ] = hashes;
        Ok(Self {
            version: 1,
            source_sha256,
            image_sha256,
            proof_sha256,
            rollback_image_sha256,
            rollback_proof_sha256,
            rollback_opened_unix_micros: opened,
            rollback_deadline_unix_micros: deadline,
        })
    }
    /// Original declaration and clock require trusted external provenance.
    /// Recomputed byte hashes do not establish proof validity or rollback safety.
    pub fn assess(
        &self,
        original: &Self,
        supplied: [&[u8]; 5],
        checked: u64,
    ) -> Result<ExportGateRefusal, ExportGateError> {
        if self != original {
            return Err(ExportGateError::IdentityMismatch);
        }
        if checked < self.rollback_opened_unix_micros {
            return Err(ExportGateError::InvalidDeclaration);
        }
        let expected = [
            &self.source_sha256,
            &self.image_sha256,
            &self.proof_sha256,
            &self.rollback_image_sha256,
            &self.rollback_proof_sha256,
        ];
        for (bytes, hash) in supplied.into_iter().zip(expected) {
            if bytes.is_empty() || hex::encode(Sha256::digest(bytes)) != *hash {
                return Err(ExportGateError::DigestMismatch);
            }
        }
        Ok(if checked >= self.rollback_deadline_unix_micros {
            ExportGateRefusal::RollbackWindowClosed
        } else {
            ExportGateRefusal::CutoverAndRollbackAuthorityUnavailable
        })
    }
}
