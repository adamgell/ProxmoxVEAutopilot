use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::{OperationId, SemanticOperationKey};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationKind {
    SyntheticLongSleep,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct CommandEnvelope {
    pub idempotency_key: String,
    pub semantic_key: SemanticOperationKey,
    /// An opaque digest validated by intake. Canonical construction belongs to event-journal.
    pub payload_digest: String,
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
    pub operation_id: OperationId,
    pub payload_digest: String,
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
        Some(existing) if existing.payload_digest == incoming.payload_digest => {
            IdempotencyDecision::ReturnExisting(existing.operation_id)
        }
        Some(existing) => IdempotencyDecision::Conflict {
            existing: existing.operation_id,
        },
    }
}
