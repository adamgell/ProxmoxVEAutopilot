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
mod store;
pub use native::{
    NativeDispatchPermit, NativeOperationSnapshot, NativeStoreError, NativeWorkflowIds,
};
pub use osdeploy::execution::{
    OsDeployExecutionError, OsDeployOperationSnapshot, OsDeployProgress,
};
pub use osdeploy::{
    OsDeployOperationPlanV1, OsDeployRegistrationV1, OsDeployStoreError, OsDeployWorkflowIds,
};

pub use scheduler::{
    AuthoritySnapshot, ExecutorKind, LeaseGrant, OsDeployDispatchPermit, OsDeployLeaseStatus,
    OsDeployResponseCapture, ReapSummary, Scheduler, SchedulerError,
};
pub use store::{
    CommandAppend, EventAppend, OperationProjection, OutboxMessage, PgStore, StoreError,
};
