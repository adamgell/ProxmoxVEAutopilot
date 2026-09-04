use thiserror::Error;

use crate::ExecutionState;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DomainSignal {
    Claimed,
    Started,
    WaitRequested,
    DeadlineElapsed,
    CancellationRequested,
    Satisfied,
    Failed,
    Blocked,
    ConflictDetected,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Transition {
    pub previous: ExecutionState,
    pub next: ExecutionState,
    pub signal: DomainSignal,
}

#[derive(Clone, Copy, Debug, Error, Eq, PartialEq)]
pub enum TransitionError {
    #[error("terminal execution state {state:?} cannot transition on {signal:?}")]
    TerminalState {
        state: ExecutionState,
        signal: DomainSignal,
    },
    #[error("signal {signal:?} is not valid from execution state {state:?}")]
    InvalidSignal {
        state: ExecutionState,
        signal: DomainSignal,
    },
}

pub fn decide_transition(
    previous: ExecutionState,
    signal: DomainSignal,
) -> Result<Transition, TransitionError> {
    if previous.is_terminal() {
        return Err(TransitionError::TerminalState {
            state: previous,
            signal,
        });
    }

    let next = match (previous, signal) {
        (ExecutionState::Pending, DomainSignal::Claimed) => ExecutionState::Leased,
        (ExecutionState::Leased, DomainSignal::Started) => ExecutionState::Running,
        (ExecutionState::Running, DomainSignal::WaitRequested) => ExecutionState::Waiting,
        (ExecutionState::Waiting, DomainSignal::Started) => ExecutionState::Running,
        (ExecutionState::Running | ExecutionState::Waiting, DomainSignal::DeadlineElapsed) => {
            ExecutionState::Unknown
        }
        (
            ExecutionState::Running | ExecutionState::Waiting,
            DomainSignal::CancellationRequested,
        ) => ExecutionState::Cancelling,
        (
            ExecutionState::Pending
            | ExecutionState::Leased
            | ExecutionState::Running
            | ExecutionState::Waiting
            | ExecutionState::Cancelling,
            DomainSignal::Satisfied,
        ) => ExecutionState::Satisfied,
        (
            ExecutionState::Pending
            | ExecutionState::Leased
            | ExecutionState::Running
            | ExecutionState::Waiting
            | ExecutionState::Cancelling,
            DomainSignal::Failed,
        ) => ExecutionState::Failed,
        (
            ExecutionState::Pending
            | ExecutionState::Leased
            | ExecutionState::Running
            | ExecutionState::Waiting
            | ExecutionState::Cancelling,
            DomainSignal::Blocked,
        ) => ExecutionState::Blocked,
        (
            ExecutionState::Pending
            | ExecutionState::Leased
            | ExecutionState::Running
            | ExecutionState::Waiting
            | ExecutionState::Cancelling,
            DomainSignal::ConflictDetected,
        ) => ExecutionState::Conflicted,
        (state, signal) => return Err(TransitionError::InvalidSignal { state, signal }),
    };

    Ok(Transition {
        previous,
        next,
        signal,
    })
}
