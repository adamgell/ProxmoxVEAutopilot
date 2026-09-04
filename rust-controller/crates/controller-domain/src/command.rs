use serde::{Deserialize, Serialize, de};
use thiserror::Error;

use crate::{OperationId, SemanticOperationKey};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationKind {
    SyntheticLongSleep,
}

/// ```compile_fail
/// use controller_domain::CommandEnvelope;
///
/// let _ = CommandEnvelope {
///     idempotency_key: String::new(),
///     semantic_key: unimplemented!(),
///     payload_digest: String::new(),
/// };
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct CommandEnvelope {
    idempotency_key: String,
    semantic_key: SemanticOperationKey,
    /// An opaque digest validated by intake. Canonical construction belongs to event-journal.
    payload_digest: String,
}

impl CommandEnvelope {
    pub fn new(
        idempotency_key: impl Into<String>,
        semantic_key: SemanticOperationKey,
        payload_digest: impl Into<String>,
    ) -> Result<Self, CommandValidationError> {
        let idempotency_key = idempotency_key.into();
        let payload_digest = payload_digest.into();

        if idempotency_key.trim().is_empty() {
            return Err(CommandValidationError::EmptyIdempotencyKey);
        }
        if payload_digest.trim().is_empty() {
            return Err(CommandValidationError::EmptyPayloadDigest);
        }
        semantic_key.validate()?;

        Ok(Self {
            idempotency_key,
            semantic_key,
            payload_digest,
        })
    }

    #[must_use]
    pub fn idempotency_key(&self) -> &str {
        &self.idempotency_key
    }

    #[must_use]
    pub const fn semantic_key(&self) -> &SemanticOperationKey {
        &self.semantic_key
    }

    #[must_use]
    pub fn payload_digest(&self) -> &str {
        &self.payload_digest
    }
}

#[derive(Deserialize)]
struct CommandEnvelopeWire {
    idempotency_key: String,
    semantic_key: SemanticOperationKey,
    payload_digest: String,
}

impl TryFrom<CommandEnvelopeWire> for CommandEnvelope {
    type Error = CommandValidationError;

    fn try_from(value: CommandEnvelopeWire) -> Result<Self, Self::Error> {
        Self::new(
            value.idempotency_key,
            value.semantic_key,
            value.payload_digest,
        )
    }
}

impl<'de> Deserialize<'de> for CommandEnvelope {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: de::Deserializer<'de>,
    {
        CommandEnvelopeWire::deserialize(deserializer)?
            .try_into()
            .map_err(de::Error::custom)
    }
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum CommandValidationError {
    #[error("idempotency key must not be empty")]
    EmptyIdempotencyKey,
    #[error("payload digest must not be empty")]
    EmptyPayloadDigest,
    #[error(transparent)]
    SemanticKey(#[from] crate::ValidationError),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PersistedCommand {
    operation_id: OperationId,
    payload_digest: String,
}

impl PersistedCommand {
    pub fn new(
        operation_id: OperationId,
        payload_digest: impl Into<String>,
    ) -> Result<Self, CommandValidationError> {
        let payload_digest = payload_digest.into();
        if payload_digest.trim().is_empty() {
            return Err(CommandValidationError::EmptyPayloadDigest);
        }

        Ok(Self {
            operation_id,
            payload_digest,
        })
    }

    #[must_use]
    pub const fn operation_id(&self) -> OperationId {
        self.operation_id
    }

    #[must_use]
    pub fn payload_digest(&self) -> &str {
        &self.payload_digest
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum IdempotencyDecision {
    AcceptNew,
    ReturnExisting(OperationId),
    Conflict { existing: OperationId },
}

#[must_use]
pub fn check_idempotency(
    existing: Option<&PersistedCommand>,
    incoming: &CommandEnvelope,
) -> IdempotencyDecision {
    match existing {
        None => IdempotencyDecision::AcceptNew,
        Some(existing) if existing.payload_digest() == incoming.payload_digest() => {
            IdempotencyDecision::ReturnExisting(existing.operation_id())
        }
        Some(existing) => IdempotencyDecision::Conflict {
            existing: existing.operation_id(),
        },
    }
}
