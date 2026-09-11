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
#[cfg(feature = "fixture-ipc")]
pub use scheduler::{
    FixtureCredentialEnvelope, FixtureCredentialSink, FixtureDeliveryAck, FixtureDeliveryRecovery,
    FixturePeRegistrationIdentity, FixturePeRegistrationResult,
};
pub mod native;
mod osdeploy;
#[cfg(feature = "fixture-ipc")]
mod pe_register_result;
#[cfg(feature = "fixture-ipc")]
mod pe_register_result_probe;
#[cfg(feature = "fixture-ipc")]
mod start_pe_session;
#[cfg(feature = "fixture-ipc")]
pub use pe_register_result::{PeRegisterResultRefusalV1, PeRegisterResultTransactionInputV1};
mod store;
pub use native::{
    NativeDispatchPermit, NativeOperationSnapshot, NativeStoreError, NativeWorkflowIds,
};
#[cfg(feature = "fixture-ipc")]
pub use osdeploy::FixtureCreatedOsDeployV1;
pub use osdeploy::execution::{
    OsDeployDue, OsDeployDueKind, OsDeployExecutionError, OsDeployExpiryCursor,
    OsDeployOperationSnapshot, OsDeployProgress, OsDeployPveObservation, OsDeployRepairCursor,
};
pub use osdeploy::{
    MaterializedPePackageSemanticsV1, OsDeployOperationPlanV1, OsDeployRegistrationV1,
    OsDeployStoreError, OsDeployWorkflowIds, RegisteredPePackageSemanticsV1,
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
