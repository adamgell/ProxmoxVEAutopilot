use std::collections::BTreeSet;

use osdeploy_adapter::{OsDeployStage, StageDependency, StageKind};
use serde_json::json;

#[test]
fn fixed_manifest_preserves_all_sixteen_operation_identities_in_order() {
    use OsDeployStage::*;
    assert_eq!(
        OsDeployStage::ALL,
        [
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
        ]
    );
    let keys = OsDeployStage::ALL.map(OsDeployStage::operation_key);
    assert_eq!(
        keys,
        [
            "osdeploy.clone.v1",
            "osdeploy.disk.capacity.v1",
            "osdeploy.configure.pe.v1",
            "osdeploy.start.pe.v1",
            "osdeploy.pe.register.v1",
            "osdeploy.pe.complete.v1",
            "osdeploy.pe.shutdown.grace.v1",
            "osdeploy.pe.ensure-stopped.v1",
            "osdeploy.configure.disk.v1",
            "osdeploy.start.disk.v1",
            "osdeploy.fullos.install-qga.v1",
            "osdeploy.fullos.verify-qga.v1",
            "osdeploy.fullos.install-qga-watchdog.v1",
            "osdeploy.fullos.install-agent.v1",
            "osdeploy.fullos.agent-heartbeat.v1",
            "osdeploy.verify-operational.v1",
        ]
    );
    assert_eq!(keys.into_iter().collect::<BTreeSet<_>>().len(), 16);
}

#[test]
fn only_ensure_stopped_has_the_guarded_escalation_dependency() {
    use OsDeployStage::*;
    use StageDependency::*;
    assert_eq!(
        OsDeployStage::ALL.map(OsDeployStage::dependency),
        [
            Intake,
            Satisfied(Clone),
            Satisfied(DiskCapacity),
            Satisfied(ConfigurePe),
            Satisfied(StartPe),
            Satisfied(PeRegister),
            Satisfied(PeComplete),
            ShutdownGraceOrGuardedEscalation,
            Satisfied(PeEnsureStopped),
            Satisfied(ConfigureDisk),
            Satisfied(StartDisk),
            Satisfied(InstallQga),
            Satisfied(VerifyQga),
            Satisfied(InstallQgaWatchdog),
            Satisfied(InstallAgent),
            Satisfied(AgentHeartbeat),
        ]
    );
}

#[test]
fn stage_kinds_preserve_mutation_callback_observation_and_guest_boundaries() {
    use StageKind::*;
    assert_eq!(
        OsDeployStage::ALL.map(OsDeployStage::kind),
        [
            PveMutation,
            PveMutation,
            PveMutation,
            PveMutation,
            CallbackWait,
            CallbackWait,
            ObservationWait,
            PveMutation,
            PveMutation,
            PveMutation,
            GuestAction,
            GuestAction,
            GuestAction,
            GuestAction,
            GuestAction,
            ObservationWait,
        ]
    );
}

#[test]
fn serialization_pins_every_stage_kind_and_dependency_encoding() {
    assert_eq!(
        serde_json::to_value(OsDeployStage::ALL).unwrap(),
        json!([
            "clone",
            "disk_capacity",
            "configure_pe",
            "start_pe",
            "pe_register",
            "pe_complete",
            "pe_shutdown_grace",
            "pe_ensure_stopped",
            "configure_disk",
            "start_disk",
            "install_qga",
            "verify_qga",
            "install_qga_watchdog",
            "install_agent",
            "agent_heartbeat",
            "verify_operational"
        ])
    );
    assert_eq!(
        serde_json::to_value(OsDeployStage::ALL.map(OsDeployStage::kind)).unwrap(),
        json!([
            "pve_mutation",
            "pve_mutation",
            "pve_mutation",
            "pve_mutation",
            "callback_wait",
            "callback_wait",
            "observation_wait",
            "pve_mutation",
            "pve_mutation",
            "pve_mutation",
            "guest_action",
            "guest_action",
            "guest_action",
            "guest_action",
            "guest_action",
            "observation_wait"
        ])
    );
    assert_eq!(
        serde_json::to_value(OsDeployStage::ALL.map(OsDeployStage::dependency)).unwrap(),
        json!([
            "intake", {"satisfied":"clone"}, {"satisfied":"disk_capacity"}, {"satisfied":"configure_pe"},
            {"satisfied":"start_pe"}, {"satisfied":"pe_register"}, {"satisfied":"pe_complete"},
            "shutdown_grace_or_guarded_escalation", {"satisfied":"pe_ensure_stopped"},
            {"satisfied":"configure_disk"}, {"satisfied":"start_disk"}, {"satisfied":"install_qga"},
            {"satisfied":"verify_qga"}, {"satisfied":"install_qga_watchdog"},
            {"satisfied":"install_agent"}, {"satisfied":"agent_heartbeat"}
        ])
    );
    // The ordinary grace predecessor is not used in ALL but remains serializable.
    assert_eq!(
        serde_json::to_value(StageDependency::Satisfied(OsDeployStage::PeShutdownGrace)).unwrap(),
        json!({"satisfied":"pe_shutdown_grace"})
    );
    assert_eq!(
        serde_json::to_value(StageDependency::Satisfied(OsDeployStage::VerifyOperational)).unwrap(),
        json!({"satisfied":"verify_operational"})
    );
}
