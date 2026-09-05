//! Native decisions and bounded orchestration over an in-memory fake only.
mod controller;
pub mod decision;
pub use controller::{NativeController, NativeControllerError, NativeProgress};
