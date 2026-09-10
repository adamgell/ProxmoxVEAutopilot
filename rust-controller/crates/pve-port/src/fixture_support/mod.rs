//! Opt-in Unix fixture daemon support for isolated process tests.
//!
//! The ledger is private; callers can run the bounded daemon and inspect its
//! protocol replies. This module grants no provisioning mutation capability.
mod durable_fixture_log;
mod fixture_daemon;

pub use durable_fixture_log::{Effect, VmState};
pub use fixture_daemon::{Reply, run};
