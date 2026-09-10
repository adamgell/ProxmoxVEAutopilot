//! Opt-in Unix fixture daemon support for isolated process tests.
//!
//! The ledger is private; callers can run the bounded daemon and inspect its
//! protocol replies. `FixtureProvisioningPort` supplies a sealed synthetic Clone
//! capability with an explicitly bound supervisor checkpoint client. Missing
//! checkpoint configuration fails closed before controller dispatch.
mod checkpoint;
mod stage_checkpoint;
mod stage_effect;
mod stage_identity;
pub use durable_fixture_log::{FixtureLedgerStage, StageBinding};
pub use stage_checkpoint::{StageCheckpointReply, StageCheckpointRequest};
pub use stage_identity::FixtureStageIdentity;
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
mod post_dispatch;
mod post_dispatch_publication;
pub use post_dispatch_publication::FixturePostDispatchPublication;
mod read_client;
mod task;
pub use clone_mutation::{FixtureCloneSeed, FixtureMutationClient};
pub use post_dispatch::FixturePostDispatchV1;
pub use task::{FixtureTaskIdentity, FixtureTaskObservation, FixtureTaskState};

pub use durable_fixture_log::{Effect, VmState};
pub use fixture_daemon::snapshot::{FixtureInventory, FixtureSnapshot, FixtureVmConfig};
pub use fixture_daemon::{Reply, run};
pub use inventory_read::{
    FixtureFact, FixtureInventoryRead, FixtureProjectionError, FixtureVmRead,
};
pub use read_client::{FixtureReadClient, FixtureStatus};
