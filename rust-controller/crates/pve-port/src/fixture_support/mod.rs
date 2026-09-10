//! Opt-in Unix fixture daemon support for isolated process tests.
//!
//! The ledger is private; callers can run the bounded daemon and inspect its
//! protocol replies. `FixtureProvisioningPort` supplies a sealed synthetic Clone
//! capability with an explicitly bound supervisor checkpoint client. Missing
//! checkpoint configuration fails closed before controller dispatch.
mod checkpoint;
mod clone_mutation;
mod late_authorization;
pub use checkpoint::{
    CheckpointBinding, CheckpointPhase, CheckpointPoint, CheckpointReply, CheckpointRequest,
    CheckpointState, FixtureCheckpointClient,
};
pub use late_authorization::{FixtureReadIdentity, LateCloneAuthorizationV1};
mod clone_reads;
mod provisioning_port;
mod provisioning_reads;
pub use clone_reads::{
    FixtureCloneReads, SeedBridge, SeedIdentity, SeedNode, SeedRead, SeedReadError, SeedStorage,
};
pub use provisioning_port::FixtureProvisioningPort;
pub use provisioning_reads::{
    FixtureProvisioningIdentity, FixtureProvisioningReads, FixtureProvisioningReadsV2, SeedConfig,
    SeedPower,
};
mod durable_fixture_log;
mod fixture_daemon;
mod inventory_read;
mod read_client;
mod task;
pub use clone_mutation::{FixtureCloneSeed, FixtureMutationClient};
pub use task::{FixtureTaskIdentity, FixtureTaskObservation, FixtureTaskState};

pub use durable_fixture_log::{Effect, VmState};
pub use fixture_daemon::snapshot::{FixtureInventory, FixtureSnapshot, FixtureVmConfig};
pub use fixture_daemon::{Reply, run};
pub use inventory_read::{
    FixtureFact, FixtureInventoryRead, FixtureProjectionError, FixtureVmRead,
};
pub use read_client::{FixtureReadClient, FixtureStatus};
