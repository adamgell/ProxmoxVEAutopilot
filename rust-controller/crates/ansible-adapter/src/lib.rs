//! The single local synthetic Ansible capability. No arbitrary command API.

mod contract;
mod process_tree;
mod runner;
pub use contract::{
    AdapterContract, AdapterError, AdapterRegistry, SYNTHETIC_LONG_SLEEP_V1, ValidatedInvocation,
};
pub use runner::{AdapterEvent, AdapterReport, AdapterRunner, CompletionReason, SanitizedLine};

#[cfg(test)]
mod lifecycle_tests;

#[cfg(test)]
mod process_tests;
