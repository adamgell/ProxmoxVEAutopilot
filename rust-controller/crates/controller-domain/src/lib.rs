mod command;
mod evidence;
mod id;
mod state;
mod transition;

pub use command::{
    CommandEnvelope, CommandValidationError, IdempotencyDecision, OperationKind, PersistedCommand,
    check_idempotency,
};
pub use evidence::{OperationAggregate, ReadinessRecordError};
pub use id::{
    AttemptId, EventId, OperationId, RunId, SemanticOperationKey, ValidationError, WorkflowKind,
};
pub use state::{ExecutionState, ObservationHealth, ReadinessMilestone};
pub use transition::{DomainSignal, Transition, TransitionError, decide_transition};

#[cfg(test)]
mod tests {
    use super::{
        AttemptId, CommandEnvelope, DomainSignal, EventId, ExecutionState, IdempotencyDecision,
        ObservationHealth, OperationAggregate, OperationId, PersistedCommand, ReadinessMilestone,
        RunId, SemanticOperationKey, WorkflowKind, check_idempotency, decide_transition,
    };
    use proptest::prelude::*;

    #[test]
    fn deadline_from_running_is_unknown_not_failed() {
        let transition =
            decide_transition(ExecutionState::Running, DomainSignal::DeadlineElapsed).unwrap();

        assert_eq!(transition.next, ExecutionState::Unknown);
    }

    #[test]
    fn readiness_recording_preserves_execution_state() {
        let mut aggregate = OperationAggregate::running_fixture();

        aggregate
            .record_readiness(ReadinessMilestone::AgentConnected)
            .unwrap();

        assert_eq!(aggregate.execution_state(), ExecutionState::Running);
    }

    #[test]
    fn opaque_ids_are_uuid_v7_values() {
        assert_eq!(RunId::new().as_uuid().get_version_num(), 7);
        assert_eq!(OperationId::new().as_uuid().get_version_num(), 7);
        assert_eq!(AttemptId::new().as_uuid().get_version_num(), 7);
        assert_eq!(EventId::new().as_uuid().get_version_num(), 7);
    }

    #[test]
    fn semantic_operation_key_rejects_blank_operation_key_and_zero_contract_version() {
        let run_id = RunId::new();
        assert!(
            SemanticOperationKey::new(WorkflowKind::SyntheticLongSleep, run_id, " ", 1).is_err()
        );
        assert!(
            SemanticOperationKey::new(WorkflowKind::SyntheticLongSleep, run_id, "run", 0).is_err()
        );
    }

    #[test]
    fn state_dimensions_serialize_only_as_their_documented_snake_case_values() {
        let execution = [
            (ExecutionState::Pending, "pending"),
            (ExecutionState::Leased, "leased"),
            (ExecutionState::Running, "running"),
            (ExecutionState::Waiting, "waiting"),
            (ExecutionState::Cancelling, "cancelling"),
            (ExecutionState::Satisfied, "satisfied"),
            (ExecutionState::Failed, "failed"),
            (ExecutionState::Blocked, "blocked"),
            (ExecutionState::Unknown, "unknown"),
            (ExecutionState::Conflicted, "conflicted"),
        ];
        let health = [
            (ObservationHealth::Fresh, "fresh"),
            (ObservationHealth::Stale, "stale"),
            (ObservationHealth::Unavailable, "unavailable"),
            (ObservationHealth::Unauthorized, "unauthorized"),
            (ObservationHealth::TimedOut, "timed_out"),
            (ObservationHealth::Contradicted, "contradicted"),
        ];
        let readiness = [
            (ReadinessMilestone::VmCreated, "vm_created"),
            (ReadinessMilestone::PeRegistered, "pe_registered"),
            (ReadinessMilestone::OsInstalled, "os_installed"),
            (ReadinessMilestone::AgentConnected, "agent_connected"),
            (ReadinessMilestone::QgaVerified, "qga_verified"),
            (ReadinessMilestone::VerifiedOobe, "verified_oobe"),
            (ReadinessMilestone::Enrolled, "enrolled"),
            (ReadinessMilestone::EspComplete, "esp_complete"),
            (ReadinessMilestone::UsableEndpoint, "usable_endpoint"),
        ];

        for (value, expected) in execution {
            assert_eq!(
                serde_json::to_string(&value).unwrap(),
                format!("\"{expected}\"")
            );
        }
        for (value, expected) in health {
            assert_eq!(
                serde_json::to_string(&value).unwrap(),
                format!("\"{expected}\"")
            );
        }
        for (value, expected) in readiness {
            assert_eq!(
                serde_json::to_string(&value).unwrap(),
                format!("\"{expected}\"")
            );
        }
    }

    #[test]
    fn idempotency_returns_existing_operation_only_for_matching_digest() {
        let incoming = CommandEnvelope::new(
            "capture-1",
            SemanticOperationKey::new(WorkflowKind::SyntheticLongSleep, RunId::new(), "run", 1)
                .unwrap(),
            "opaque-validated-digest",
        )
        .unwrap();
        let existing = PersistedCommand {
            operation_id: OperationId::new(),
            payload_digest: "opaque-validated-digest".into(),
        };

        assert_eq!(
            check_idempotency(Some(&existing), &incoming),
            IdempotencyDecision::ReturnExisting(existing.operation_id)
        );

        let conflicting = CommandEnvelope::new(
            "capture-1",
            incoming.semantic_key.clone(),
            "a-different-opaque-digest",
        )
        .unwrap();
        assert_eq!(
            check_idempotency(Some(&existing), &conflicting),
            IdempotencyDecision::Conflict {
                existing: existing.operation_id
            }
        );
    }

    #[test]
    fn terminal_execution_states_cannot_regress() {
        for terminal in [
            ExecutionState::Satisfied,
            ExecutionState::Failed,
            ExecutionState::Blocked,
            ExecutionState::Unknown,
            ExecutionState::Conflicted,
        ] {
            assert!(decide_transition(terminal, DomainSignal::Started).is_err());
        }
    }

    #[test]
    fn cancellation_from_running_becomes_cancelling() {
        let transition =
            decide_transition(ExecutionState::Running, DomainSignal::CancellationRequested)
                .unwrap();

        assert_eq!(transition.next, ExecutionState::Cancelling);
    }

    proptest! {
        #[test]
        fn deadline_from_running_or_waiting_never_becomes_failed(
            state in prop_oneof![Just(ExecutionState::Running), Just(ExecutionState::Waiting)]
        ) {
            let transition = decide_transition(state, DomainSignal::DeadlineElapsed).unwrap();
            prop_assert_eq!(transition.next, ExecutionState::Unknown);
            prop_assert_ne!(transition.next, ExecutionState::Failed);
        }

        #[test]
        fn readiness_never_changes_execution_state(
            state in prop_oneof![
                Just(ExecutionState::Pending), Just(ExecutionState::Leased),
                Just(ExecutionState::Running), Just(ExecutionState::Waiting),
                Just(ExecutionState::Cancelling), Just(ExecutionState::Satisfied),
                Just(ExecutionState::Failed), Just(ExecutionState::Blocked),
                Just(ExecutionState::Unknown), Just(ExecutionState::Conflicted),
            ],
            milestone in prop_oneof![
                Just(ReadinessMilestone::VmCreated), Just(ReadinessMilestone::PeRegistered),
                Just(ReadinessMilestone::OsInstalled), Just(ReadinessMilestone::AgentConnected),
                Just(ReadinessMilestone::QgaVerified), Just(ReadinessMilestone::VerifiedOobe),
                Just(ReadinessMilestone::Enrolled), Just(ReadinessMilestone::EspComplete),
                Just(ReadinessMilestone::UsableEndpoint),
            ]
        ) {
            let mut aggregate = OperationAggregate::new(state);
            aggregate.record_readiness(milestone).unwrap();
            prop_assert_eq!(aggregate.execution_state(), state);
        }
    }
}
