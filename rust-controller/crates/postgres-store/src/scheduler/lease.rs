//! Scheduler lease value types.

use std::fmt;

use chrono::{DateTime, Utc};
use controller_domain::{AttemptId, OperationId};
use uuid::Uuid;

use crate::ExecutorKind;

#[derive(Clone, Eq, PartialEq)]
pub struct LeaseGrant {
    pub(crate) operation_id: OperationId,
    pub(crate) attempt_id: AttemptId,
    pub(crate) attempt_number: i32,
    pub(crate) executor_kind: ExecutorKind,
    pub(crate) generation: i64,
    pub(crate) worker_id: String,
    pub(crate) lease_token: Uuid,
    pub(crate) acquired_at: DateTime<Utc>,
    pub(crate) heartbeat_at: DateTime<Utc>,
    pub(crate) lease_expires_at: DateTime<Utc>,
    pub(crate) deadline_at: DateTime<Utc>,
}

impl fmt::Debug for LeaseGrant {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LeaseGrant")
            .field("operation_id", &self.operation_id)
            .field("attempt_id", &self.attempt_id)
            .field("attempt_number", &self.attempt_number)
            .field("executor_kind", &self.executor_kind)
            .field("generation", &self.generation)
            .field("worker_id", &self.worker_id)
            .field("lease_token", &"<redacted>")
            .field("acquired_at", &self.acquired_at)
            .field("heartbeat_at", &self.heartbeat_at)
            .field("lease_expires_at", &self.lease_expires_at)
            .field("deadline_at", &self.deadline_at)
            .finish()
    }
}

impl LeaseGrant {
    #[allow(clippy::too_many_arguments)]
    pub(crate) const fn new(
        operation_id: OperationId,
        attempt_id: AttemptId,
        attempt_number: i32,
        executor_kind: ExecutorKind,
        generation: i64,
        worker_id: String,
        lease_token: Uuid,
        acquired_at: DateTime<Utc>,
        heartbeat_at: DateTime<Utc>,
        lease_expires_at: DateTime<Utc>,
        deadline_at: DateTime<Utc>,
    ) -> Self {
        Self {
            operation_id,
            attempt_id,
            attempt_number,
            executor_kind,
            generation,
            worker_id,
            lease_token,
            acquired_at,
            heartbeat_at,
            lease_expires_at,
            deadline_at,
        }
    }

    #[must_use]
    pub const fn operation_id(&self) -> OperationId {
        self.operation_id
    }

    #[must_use]
    pub const fn attempt_id(&self) -> AttemptId {
        self.attempt_id
    }

    #[must_use]
    pub const fn attempt_number(&self) -> i32 {
        self.attempt_number
    }

    #[must_use]
    pub const fn executor_kind(&self) -> ExecutorKind {
        self.executor_kind
    }

    #[must_use]
    pub const fn generation(&self) -> i64 {
        self.generation
    }

    #[must_use]
    pub fn worker_id(&self) -> &str {
        &self.worker_id
    }

    #[must_use]
    pub const fn lease_token(&self) -> Uuid {
        self.lease_token
    }

    #[must_use]
    pub const fn acquired_at(&self) -> &DateTime<Utc> {
        &self.acquired_at
    }

    #[must_use]
    pub const fn heartbeat_at(&self) -> &DateTime<Utc> {
        &self.heartbeat_at
    }

    #[must_use]
    pub const fn lease_expires_at(&self) -> &DateTime<Utc> {
        &self.lease_expires_at
    }

    #[must_use]
    pub const fn deadline_at(&self) -> &DateTime<Utc> {
        &self.deadline_at
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ReapSummary {
    pub(crate) reset_to_pending: u64,
    pub(crate) marked_unknown: u64,
}

impl ReapSummary {
    #[must_use]
    pub const fn reset_to_pending(self) -> u64 {
        self.reset_to_pending
    }

    #[must_use]
    pub const fn marked_unknown(self) -> u64 {
        self.marked_unknown
    }
}
