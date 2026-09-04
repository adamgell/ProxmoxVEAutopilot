use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionState {
    Pending,
    Leased,
    Running,
    Waiting,
    Cancelling,
    Satisfied,
    Failed,
    Blocked,
    Unknown,
    Conflicted,
}

impl ExecutionState {
    #[must_use]
    pub const fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Satisfied | Self::Failed | Self::Blocked | Self::Unknown | Self::Conflicted
        )
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObservationHealth {
    Fresh,
    Stale,
    Unavailable,
    Unauthorized,
    TimedOut,
    Contradicted,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReadinessMilestone {
    VmCreated,
    PeRegistered,
    OsInstalled,
    AgentConnected,
    QgaVerified,
    VerifiedOobe,
    Enrolled,
    EspComplete,
    UsableEndpoint,
}
