use serde::Serialize;
use uuid::Uuid;

use crate::ArtifactError;

const CONTRACT_VERSION: u32 = 1;
const EVIDENCE_LEVEL: &str = "declared_only";

/// Native-v1 declarations, not a parser for legacy manifests or API rows.
pub struct DeclaredArtifactInput<'a> {
    pub artifact_id: Uuid,
    pub architecture: &'a str,
    pub build_label: &'a str,
    pub iso_sha256: &'a str,
    pub wim_sha256: &'a str,
    pub source_image_index: u32,
    pub output_image_index: Option<u32>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ImageIndexSource {
    OutputManifest,
    SourceFallback,
}

/// Validated declarations confer no publication or execution authority.
///
/// Deserialization cannot bypass admission:
/// ```compile_fail
/// use artifact_index::DeclaredArtifact;
/// let _: DeclaredArtifact = serde_json::from_str("{}").unwrap();
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(into = "DescriptorContract")]
pub struct DeclaredArtifact {
    artifact_id: Uuid,
    architecture: String,
    build_label: String,
    iso_sha256: String,
    wim_sha256: String,
    source_image_index: u32,
    output_image_index: Option<u32>,
}

impl DeclaredArtifact {
    pub fn new(input: DeclaredArtifactInput<'_>) -> Result<Self, ArtifactError> {
        if input.artifact_id.is_nil() {
            return Err(ArtifactError::InvalidArtifactId);
        }
        if input.architecture != "amd64" {
            return Err(ArtifactError::InvalidArchitecture);
        }
        if input.build_label.len() != 14
            || !input.build_label.bytes().all(|byte| byte.is_ascii_digit())
        {
            return Err(ArtifactError::InvalidBuildLabel);
        }
        if !is_sha256(input.iso_sha256) {
            return Err(ArtifactError::InvalidIsoSha256);
        }
        if !is_sha256(input.wim_sha256) {
            return Err(ArtifactError::InvalidWimSha256);
        }
        if input.source_image_index == 0 {
            return Err(ArtifactError::InvalidSourceImageIndex);
        }
        if input.output_image_index == Some(0) {
            return Err(ArtifactError::InvalidOutputImageIndex);
        }
        Ok(Self {
            artifact_id: input.artifact_id,
            architecture: input.architecture.to_owned(),
            build_label: input.build_label.to_owned(),
            iso_sha256: input.iso_sha256.to_ascii_lowercase(),
            wim_sha256: input.wim_sha256.to_ascii_lowercase(),
            source_image_index: input.source_image_index,
            output_image_index: input.output_image_index,
        })
    }

    pub fn artifact_id(&self) -> Uuid {
        self.artifact_id
    }
    pub fn architecture(&self) -> &str {
        &self.architecture
    }
    pub fn build_label(&self) -> &str {
        &self.build_label
    }
    pub fn iso_sha256(&self) -> &str {
        &self.iso_sha256
    }
    pub fn wim_sha256(&self) -> &str {
        &self.wim_sha256
    }
    pub fn source_image_index(&self) -> u32 {
        self.source_image_index
    }
    pub fn output_image_index(&self) -> Option<u32> {
        self.output_image_index
    }
    pub fn apply_image_index(&self) -> u32 {
        self.output_image_index.unwrap_or(self.source_image_index)
    }
    pub fn image_index_source(&self) -> ImageIndexSource {
        match self.output_image_index {
            Some(_) => ImageIndexSource::OutputManifest,
            None => ImageIndexSource::SourceFallback,
        }
    }

    /// Canonical digest of the complete declared-only serialized contract.
    pub fn fingerprint(&self) -> Result<String, ArtifactError> {
        let value = serde_json::to_value(self).map_err(|_| ArtifactError::FingerprintFailed)?;
        event_journal::payload_digest(&value).map_err(|_| ArtifactError::FingerprintFailed)
    }
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

// Derived fields and evidence constants exist only at the serialization boundary.
#[derive(Serialize)]
struct DescriptorContract {
    contract_version: u32,
    artifact_id: Uuid,
    architecture: String,
    build_label: String,
    iso_sha256: String,
    wim_sha256: String,
    source_image_index: u32,
    output_image_index: Option<u32>,
    apply_image_index: u32,
    image_index_source: ImageIndexSource,
    evidence_level: &'static str,
}

impl From<DeclaredArtifact> for DescriptorContract {
    fn from(artifact: DeclaredArtifact) -> Self {
        let apply_image_index = artifact.apply_image_index();
        let image_index_source = artifact.image_index_source();
        Self {
            contract_version: CONTRACT_VERSION,
            artifact_id: artifact.artifact_id,
            architecture: artifact.architecture,
            build_label: artifact.build_label,
            iso_sha256: artifact.iso_sha256,
            wim_sha256: artifact.wim_sha256,
            source_image_index: artifact.source_image_index,
            output_image_index: artifact.output_image_index,
            apply_image_index,
            image_index_source,
            evidence_level: EVIDENCE_LEVEL,
        }
    }
}
