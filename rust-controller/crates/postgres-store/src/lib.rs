//! PostgreSQL persistence exposed only through invariant-preserving operations.
//!
//! The underlying SQLx pool is deliberately unavailable to consumers.
//!
//! ```compile_fail
//! use postgres_store::PgStore;
//! use sqlx::PgPool;
//!
//! let pool: PgPool = todo!();
//! let store = PgStore::new(pool);
//! let _unrestricted = store.pool();
//! ```

mod scheduler;
mod store;

pub use scheduler::{
    AuthoritySnapshot, ExecutorKind, LeaseGrant, ReapSummary, Scheduler, SchedulerError,
};
pub use store::{
    CommandAppend, EventAppend, OperationProjection, OutboxMessage, PgStore, StoreError,
};
