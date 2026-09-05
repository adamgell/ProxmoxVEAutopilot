use serde::Serialize;

/// Closed native OSDeploy manifest. These identities confer no execution authority.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OsDeployStage {
    Clone,
    DiskCapacity,
    ConfigurePe,
    StartPe,
    PeRegister,
    PeComplete,
    PeShutdownGrace,
    PeEnsureStopped,
    ConfigureDisk,
    StartDisk,
    InstallQga,
    VerifyQga,
    InstallQgaWatchdog,
    InstallAgent,
    AgentHeartbeat,
    VerifyOperational,
}

/// Work classification, not a dispatch decision.
///
/// Capacity and ensure-stopped may satisfy from verified no-change facts.
/// Agent heartbeat includes a guest action plus independent heartbeat evaluation.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StageKind {
    PveMutation,
    CallbackWait,
    ObservationWait,
    GuestAction,
}

/// Fixed prerequisite shape. Grace expiry remains Unknown; the special edge
/// describes later guarded evaluation and does not itself authorize escalation.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StageDependency {
    Intake,
    Satisfied(OsDeployStage),
    ShutdownGraceOrGuardedEscalation,
}

impl OsDeployStage {
    pub const ALL: [Self; 16] = [
        Self::Clone,
        Self::DiskCapacity,
        Self::ConfigurePe,
        Self::StartPe,
        Self::PeRegister,
        Self::PeComplete,
        Self::PeShutdownGrace,
        Self::PeEnsureStopped,
        Self::ConfigureDisk,
        Self::StartDisk,
        Self::InstallQga,
        Self::VerifyQga,
        Self::InstallQgaWatchdog,
        Self::InstallAgent,
        Self::AgentHeartbeat,
        Self::VerifyOperational,
    ];

    pub const fn operation_key(self) -> &'static str {
        match self {
            Self::Clone => "osdeploy.clone.v1",
            Self::DiskCapacity => "osdeploy.disk.capacity.v1",
            Self::ConfigurePe => "osdeploy.configure.pe.v1",
            Self::StartPe => "osdeploy.start.pe.v1",
            Self::PeRegister => "osdeploy.pe.register.v1",
            Self::PeComplete => "osdeploy.pe.complete.v1",
            Self::PeShutdownGrace => "osdeploy.pe.shutdown.grace.v1",
            Self::PeEnsureStopped => "osdeploy.pe.ensure-stopped.v1",
            Self::ConfigureDisk => "osdeploy.configure.disk.v1",
            Self::StartDisk => "osdeploy.start.disk.v1",
            Self::InstallQga => "osdeploy.fullos.install-qga.v1",
            Self::VerifyQga => "osdeploy.fullos.verify-qga.v1",
            Self::InstallQgaWatchdog => "osdeploy.fullos.install-qga-watchdog.v1",
            Self::InstallAgent => "osdeploy.fullos.install-agent.v1",
            Self::AgentHeartbeat => "osdeploy.fullos.agent-heartbeat.v1",
            Self::VerifyOperational => "osdeploy.verify-operational.v1",
        }
    }

    pub const fn kind(self) -> StageKind {
        match self {
            Self::Clone
            | Self::DiskCapacity
            | Self::ConfigurePe
            | Self::StartPe
            | Self::PeEnsureStopped
            | Self::ConfigureDisk
            | Self::StartDisk => StageKind::PveMutation,
            Self::PeRegister | Self::PeComplete => StageKind::CallbackWait,
            Self::PeShutdownGrace | Self::VerifyOperational => StageKind::ObservationWait,
            Self::InstallQga
            | Self::VerifyQga
            | Self::InstallQgaWatchdog
            | Self::InstallAgent
            | Self::AgentHeartbeat => StageKind::GuestAction,
        }
    }

    pub const fn dependency(self) -> StageDependency {
        match self {
            Self::Clone => StageDependency::Intake,
            Self::DiskCapacity => StageDependency::Satisfied(Self::Clone),
            Self::ConfigurePe => StageDependency::Satisfied(Self::DiskCapacity),
            Self::StartPe => StageDependency::Satisfied(Self::ConfigurePe),
            Self::PeRegister => StageDependency::Satisfied(Self::StartPe),
            Self::PeComplete => StageDependency::Satisfied(Self::PeRegister),
            Self::PeShutdownGrace => StageDependency::Satisfied(Self::PeComplete),
            Self::PeEnsureStopped => StageDependency::ShutdownGraceOrGuardedEscalation,
            Self::ConfigureDisk => StageDependency::Satisfied(Self::PeEnsureStopped),
            Self::StartDisk => StageDependency::Satisfied(Self::ConfigureDisk),
            Self::InstallQga => StageDependency::Satisfied(Self::StartDisk),
            Self::VerifyQga => StageDependency::Satisfied(Self::InstallQga),
            Self::InstallQgaWatchdog => StageDependency::Satisfied(Self::VerifyQga),
            Self::InstallAgent => StageDependency::Satisfied(Self::InstallQgaWatchdog),
            Self::AgentHeartbeat => StageDependency::Satisfied(Self::InstallAgent),
            Self::VerifyOperational => StageDependency::Satisfied(Self::AgentHeartbeat),
        }
    }
}
