//! Scheduler domain façade backed by the PostgreSQL store's fenced operations.

mod authority;
mod lease;

pub use authority::{AuthoritySnapshot, ExecutorKind};
pub use lease::{LeaseGrant, ReapSummary};
pub use postgres_store::{Scheduler, SchedulerError};
