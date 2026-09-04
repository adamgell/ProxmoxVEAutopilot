use chrono::{DateTime, Utc};
use controller_domain::{AttemptId, EventId, ExecutionState, OperationId};
use serde::{Deserialize, Serialize, de};
use serde_json::Value;
use thiserror::Error;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventKind {
    CommandAccepted,
    AttemptStarted,
    ExecutionStateChanged(ExecutionState),
    EvidenceRecorded,
    DecisionRecorded,
}

/// ```compile_fail
/// use chrono::Utc;
/// use controller_domain::{EventId, OperationId};
/// use event_journal::{EventKind, JournalEvent};
///
/// let _ = JournalEvent {
///     event_id: EventId::new(),
///     operation_id: OperationId::new(),
///     attempt_id: None,
///     aggregate_revision: 1,
///     semantic_key: "callback:ready".to_owned(),
///     payload_digest: "not-a-validated-digest".to_owned(),
///     kind: EventKind::EvidenceRecorded,
///     payload: serde_json::json!({}),
///     observed_at: Utc::now(),
/// };
/// ```
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct JournalEvent {
    event_id: EventId,
    operation_id: OperationId,
    attempt_id: Option<AttemptId>,
    aggregate_revision: i64,
    semantic_key: String,
    payload_digest: String,
    kind: EventKind,
    payload: Value,
    observed_at: DateTime<Utc>,
}

impl JournalEvent {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        event_id: EventId,
        operation_id: OperationId,
        attempt_id: Option<AttemptId>,
        aggregate_revision: i64,
        semantic_key: impl Into<String>,
        payload_digest: impl Into<String>,
        kind: EventKind,
        payload: Value,
        observed_at: DateTime<Utc>,
    ) -> Result<Self, EventValidationError> {
        let semantic_key = semantic_key.into();
        let payload_digest = payload_digest.into();

        if semantic_key.trim().is_empty() {
            return Err(EventValidationError::EmptySemanticKey);
        }
        if aggregate_revision < 1 {
            return Err(EventValidationError::AggregateRevisionBelowOne);
        }
        if payload_digest != crate::payload_digest(&payload)? {
            return Err(EventValidationError::PayloadDigestMismatch);
        }

        Ok(Self {
            event_id,
            operation_id,
            attempt_id,
            aggregate_revision,
            semantic_key,
            payload_digest,
            kind,
            payload,
            observed_at,
        })
    }

    #[must_use]
    pub const fn event_id(&self) -> EventId {
        self.event_id
    }

    #[must_use]
    pub const fn operation_id(&self) -> OperationId {
        self.operation_id
    }

    #[must_use]
    pub const fn attempt_id(&self) -> Option<AttemptId> {
        self.attempt_id
    }

    #[must_use]
    pub const fn aggregate_revision(&self) -> i64 {
        self.aggregate_revision
    }

    #[must_use]
    pub fn semantic_key(&self) -> &str {
        &self.semantic_key
    }

    #[must_use]
    pub fn payload_digest(&self) -> &str {
        &self.payload_digest
    }

    #[must_use]
    pub const fn kind(&self) -> EventKind {
        self.kind
    }

    #[must_use]
    pub const fn payload(&self) -> &Value {
        &self.payload
    }

    #[must_use]
    pub const fn observed_at(&self) -> &DateTime<Utc> {
        &self.observed_at
    }
}

#[derive(Deserialize)]
struct JournalEventWire {
    event_id: EventId,
    operation_id: OperationId,
    attempt_id: Option<AttemptId>,
    aggregate_revision: i64,
    semantic_key: String,
    payload_digest: String,
    kind: EventKind,
    payload: Value,
    observed_at: DateTime<Utc>,
}

impl TryFrom<JournalEventWire> for JournalEvent {
    type Error = EventValidationError;

    fn try_from(value: JournalEventWire) -> Result<Self, Self::Error> {
        Self::new(
            value.event_id,
            value.operation_id,
            value.attempt_id,
            value.aggregate_revision,
            value.semantic_key,
            value.payload_digest,
            value.kind,
            value.payload,
            value.observed_at,
        )
    }
}

impl<'de> Deserialize<'de> for JournalEvent {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: de::Deserializer<'de>,
    {
        JournalEventWire::deserialize(deserializer)?
            .try_into()
            .map_err(de::Error::custom)
    }
}

#[derive(Debug, Error)]
pub enum EventValidationError {
    #[error("event semantic key must not be empty")]
    EmptySemanticKey,
    #[error("aggregate revision must be at least one")]
    AggregateRevisionBelowOne,
    #[error("payload digest must match the canonical payload digest")]
    PayloadDigestMismatch,
    #[error(transparent)]
    Canonicalization(#[from] crate::CanonicalizationError),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AppendDecision {
    Append,
    AlreadyPresent(EventId),
    Conflict { existing: EventId },
}

#[must_use]
pub fn decide_append(existing: Option<&JournalEvent>, incoming: &JournalEvent) -> AppendDecision {
    match existing {
        None => AppendDecision::Append,
        Some(existing)
            if existing.operation_id() != incoming.operation_id()
                || existing.semantic_key() != incoming.semantic_key() =>
        {
            AppendDecision::Append
        }
        Some(existing) if existing.payload_digest() == incoming.payload_digest() => {
            AppendDecision::AlreadyPresent(existing.event_id())
        }
        Some(existing) => AppendDecision::Conflict {
            existing: existing.event_id(),
        },
    }
}
