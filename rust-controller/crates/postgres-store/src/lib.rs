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

mod health;
mod scheduler;
pub use health::StoreHealthSnapshot;
pub mod native;
mod osdeploy;
#[cfg(feature = "fixture-ipc")]
mod start_pe_session;
mod store;
pub use native::{
    NativeDispatchPermit, NativeOperationSnapshot, NativeStoreError, NativeWorkflowIds,
};
pub use osdeploy::execution::{
    OsDeployDue, OsDeployDueKind, OsDeployExecutionError, OsDeployExpiryCursor,
    OsDeployOperationSnapshot, OsDeployProgress, OsDeployPveObservation, OsDeployRepairCursor,
};
pub use osdeploy::{
    OsDeployOperationPlanV1, OsDeployRegistrationV1, OsDeployStoreError, OsDeployWorkflowIds,
};
#[cfg(feature = "fixture-ipc")]
pub use start_pe_session::START_PE_SESSION_SCHEMA_V1;

pub use scheduler::{
    AuthoritySnapshot, ExecutorKind, LeaseGrant, OsDeployDispatchPermit, OsDeployLeaseStatus,
    OsDeployMaintenanceSummary, OsDeployResponseCapture, ReapSummary, Scheduler, SchedulerError,
};
pub use store::{
    CommandAppend, EventAppend, OperationProjection, OutboxMessage, PgStore, StoreError,
};
