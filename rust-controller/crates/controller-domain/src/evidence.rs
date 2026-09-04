use std::collections::BTreeSet;

use thiserror::Error;

use crate::{ExecutionState, ReadinessMilestone};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OperationAggregate {
    execution_state: ExecutionState,
    readiness: BTreeSet<ReadinessMilestone>,
}

impl OperationAggregate {
    #[must_use]
    pub fn new(execution_state: ExecutionState) -> Self {
        Self {
            execution_state,
            readiness: BTreeSet::new(),
        }
    }

    #[must_use]
    pub fn running_fixture() -> Self {
        Self::new(ExecutionState::Running)
    }

    pub fn record_readiness(
        &mut self,
        milestone: ReadinessMilestone,
    ) -> Result<(), ReadinessRecordError> {
        self.readiness.insert(milestone);
        Ok(())
    }

    #[must_use]
    pub const fn execution_state(&self) -> ExecutionState {
        self.execution_state
    }

    #[must_use]
    pub fn readiness(&self) -> &BTreeSet<ReadinessMilestone> {
        &self.readiness
    }
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum ReadinessRecordError {}
