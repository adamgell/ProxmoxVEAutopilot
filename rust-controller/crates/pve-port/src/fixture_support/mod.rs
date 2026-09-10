//! Opt-in Unix fixture daemon support for isolated process tests.
//!
//! The ledger is private; callers can run the bounded daemon and inspect its
//! protocol replies. This module grants no provisioning mutation capability.
mod clone_mutation;
mod clone_reads;
mod provisioning_reads;
pub use clone_reads::{
    FixtureCloneReads, SeedBridge, SeedIdentity, SeedNode, SeedRead, SeedReadError, SeedStorage,
};
pub use provisioning_reads::{
    FixtureProvisioningIdentity, FixtureProvisioningReads, SeedConfig, SeedPower,
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
