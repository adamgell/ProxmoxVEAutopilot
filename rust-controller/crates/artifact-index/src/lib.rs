//! Pure declared artifact identity and supplied-byte comparison.

mod bytes;
mod export_gate;
pub use export_gate::{ExportGateError, ExportGateRefusal, ExportIntegrityV1};
mod descriptor;

pub use bytes::{SuppliedByteMatch, compare_supplied_bytes};
pub use descriptor::{DeclaredArtifact, DeclaredArtifactInput, ImageIndexSource};

/// Fixed errors retain neither rejected metadata nor supplied content.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum ArtifactError {
    #[error("artifact ID must be non-nil")]
    InvalidArtifactId,
    #[error("architecture must be amd64")]
    InvalidArchitecture,
    #[error("build label must contain exactly 14 ASCII digits")]
    InvalidBuildLabel,
    #[error("ISO SHA-256 must contain exactly 64 ASCII hexadecimal characters")]
    InvalidIsoSha256,
    #[error("WIM SHA-256 must contain exactly 64 ASCII hexadecimal characters")]
    InvalidWimSha256,
    #[error("source image index must be positive")]
    InvalidSourceImageIndex,
    #[error("output image index must be positive when supplied")]
    InvalidOutputImageIndex,
    #[error("descriptor fingerprint could not be computed")]
    FingerprintFailed,
    #[error("supplied ISO bytes must be nonempty")]
    EmptyIso,
    #[error("supplied WIM bytes must be nonempty")]
    EmptyWim,
    #[error("supplied ISO bytes do not match the declared digest")]
    IsoMismatch,
    #[error("supplied WIM bytes do not match the declared digest")]
    WimMismatch,
}
