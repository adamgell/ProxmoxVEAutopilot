//! Closed durable wire values. These are crate-private data, never authority.
use super::OsDeployExecutionError as Error;
use chrono::{DateTime, Utc};
use controller_domain::{AttemptId, EventId, OperationId, RunId};
use pve_port::{NativeDecision, ProvisioningReasonV1};
use serde::{Deserialize, Serialize, de};
use serde_json::Value;

pub(crate) const DECISION_LIMIT: usize = 65_536;
pub(crate) const EVIDENCE_LIMIT: usize = 1_048_576;

fn required_nullable<'de, D, T>(d: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(d)
}

macro_rules! closed_enum {
    ($name:ident { $($variant:ident),+ $(,)? }) => {
        #[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
        #[serde(rename_all = "snake_case")]
        pub(crate) enum $name { $($variant),+ }
    };
}
closed_enum!(Scope {
    MutationClone,
    MutationDiskCapacity,
    MutationConfigurePe,
    MutationStartPe,
    MutationPeEnsureStopped,
    MutationConfigureDisk,
    MutationStartDisk,
    PeRegistration,
    PeCompletion,
    ShutdownGrace,
    FullOs
});
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
#[allow(
    clippy::enum_variant_names,
    reason = "names match the pinned durable purpose discriminators"
)]
pub(crate) enum Purpose {
    InitialEvaluation,
    ResumeEvaluation,
    ReclaimedEvaluation,
    ReclaimedCredentialDelivery,
}
closed_enum!(Activity {
    PreflightRead,
    OutcomeRead
});
closed_enum!(Mode {
    Preflight,
    Outcome,
    Reconciliation
});
closed_enum!(ScheduleMode {
    Waiting,
    UnknownReconciliation
});
closed_enum!(Reason {
    LeaseExpiredBeforeStart,
    LeaseExpiredBeforeCredentialAck,
    ReadOnlyEvaluatorLeaseExpired,
    InheritedScopeDeadlineExpired,
    PhaseDeadlineExpired,
    ShutdownGraceDeadlineExpired,
    RunCancellationRequested,
    RunCancelledBeforeExposure,
    RunCancelledOutcomeUncertain,
    TerminalLeaseRevoked,
    OriginalDispatchUnresolved,
    OriginalReceiptCaptured,
    ObservationUnavailable,
    ReconciliationStillUnknown,
    LeaseExpiredAfterDispatch
});

macro_rules! closed {
    ($name:ident { $($(#[$attr:meta])* $field:ident : $ty:ty),* $(,)? }) => {
        #[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
        #[serde(deny_unknown_fields)]
        pub(crate) struct $name { $($(#[$attr])* pub(crate) $field: $ty),* }
    };
}
closed!(Schedule {
    mode: ScheduleMode,
    #[serde(deserialize_with="required_nullable")] next_check_at: Option<DateTime<Utc>>,
    unavailable_count: u8,
});
closed!(Activation {
    scope_key: Scope, anchor_operation_id: OperationId, anchor_event_id: EventId,
    opened_at: DateTime<Utc>, budget_seconds: u32, deadline_at: DateTime<Utc>,
    #[serde(deserialize_with="required_nullable")] predecessor_operation_id: Option<OperationId>,
    #[serde(deserialize_with="required_nullable")] predecessor_decision_event_id: Option<EventId>,
});
closed!(FixtureRegistration {
    lease_acquisition_event_id: EventId,
    start_operation_id: OperationId,
    dispatch_event_id: EventId,
    identity_sha256: String,
    package_sha256: String,
    registration_deadline: DateTime<Utc>,
});
closed!(FixtureCompletion {
    lease_acquisition_event_id: EventId,
    registration_event_id: EventId,
    report_sha256: String,
    definition_sha256: String,
    completion_deadline: DateTime<Utc>,
    succeeded: bool,
});
closed!(FixtureGrace {
    completion_event_id: EventId,
    deadline_at: DateTime<Utc>,
});
closed!(Acquisition {
    purpose: Purpose, acquisition_event_id: EventId, token_sha256: String, worker_id: String,
    acquired_at: DateTime<Utc>, expires_at: DateTime<Utc>, deadline_at: DateTime<Utc>,
    #[serde(deserialize_with="required_nullable")] prior_schedule_event_id: Option<EventId>,
});
closed!(Started {
    lease_acquisition_event_id: EventId,
    activity: Activity
});
closed!(Renewed { lease_acquisition_event_id: EventId, heartbeat_at: DateTime<Utc>,
    expires_at: DateTime<Utc>, deadline_at: DateTime<Utc> });
closed!(Dispatch { preflight_event_id: EventId, pve_plan_sha256: String,
    request_sha256: String, dispatched_at: DateTime<Utc>, lease_acquisition_event_id: EventId });
closed!(Evaluated { mode: Mode, advice: NativeDecision, evidence_event_id: EventId,
    evidence_sha256: String, scope_key: Scope, deadline_at: DateTime<Utc>, reason: ProvisioningReasonV1,
    #[serde(deserialize_with="required_nullable")] lease_acquisition_event_id: Option<EventId>,
    #[serde(deserialize_with="required_nullable")] schedule: Option<Schedule>,
});
closed!(Reclaimed { lease_acquisition_event_id: EventId, scope_key: Scope,
    deadline_at: DateTime<Utc>, reason: Reason });
closed!(Reparked { lease_acquisition_event_id: EventId, activity_event_id: EventId,
    scope_key: Scope, deadline_at: DateTime<Utc>, reason: Reason, schedule: Schedule });
closed!(UnactivatedExpiry { scope_key: Scope, anchor_operation_id: OperationId,
    anchor_event_id: EventId, deadline_at: DateTime<Utc>, reason: Reason });
closed!(ActivatedExpiry { scope_key: Scope, anchor_operation_id: OperationId,
    anchor_event_id: EventId, deadline_at: DateTime<Utc>,
    #[serde(deserialize_with="required_nullable")] pe_complete_operation_id: Option<OperationId>,
    #[serde(deserialize_with="required_nullable")] pe_complete_decision_event_id: Option<EventId>,
    reason: Reason });
closed!(RunCancelled { reason: Reason });
closed!(UnexposedCancellation { cancellation_event_id: EventId,
    #[serde(deserialize_with="required_nullable")] scope_key: Option<Scope>,
    #[serde(deserialize_with="required_nullable")] deadline_at: Option<DateTime<Utc>>,
    reason: Reason });
closed!(ExposedCancellation { cancellation_event_id: EventId, scope_key: Scope,
    deadline_at: DateTime<Utc>, dispatch_event_id: EventId, reason: Reason });
closed!(Revoked {
    terminal_decision_event_id: EventId,
    lease_acquisition_event_id: EventId,
    reason: Reason
});
closed!(ReconciliationSchedule { terminal_decision_event_id: EventId, dispatch_event_id: EventId,
    scope_key: Scope, deadline_at: DateTime<Utc>, reason: Reason, schedule: Schedule });
closed!(UncertainExpiry { lease_acquisition_event_id: EventId, dispatch_event_id: EventId,
    scope_key: Scope, deadline_at: DateTime<Utc>, reason: Reason });

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(
    tag = "action",
    content = "detail",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub(crate) enum Detail {
    StageActivated(Activation),
    LeaseAcquired(Acquisition),
    EvaluationStarted(Started),
    LeaseRenewed(Renewed),
    PveDispatchCommitted(Dispatch),
    PveEvaluated(Evaluated),
    FixturePeRegistered(FixtureRegistration),
    FixturePeCompleted(FixtureCompletion),
    FixtureGraceWaiting(FixtureGrace),
    LeaseReclaimedSameAttempt(Reclaimed),
    CredentialDeliveryReclaimed(Reclaimed),
    EvaluationReparked(Reparked),
    ScopeExpiredBeforeActivation(UnactivatedExpiry),
    ActivatedScopeExpired(ActivatedExpiry),
    RunCancelled(RunCancelled),
    StageCancelledUnexposed(UnexposedCancellation),
    StageCancelledExposed(ExposedCancellation),
    ResidualLeaseRevoked(Revoked),
    ReconciliationScheduled(ReconciliationSchedule),
    LeaseExpiredUncertain(UncertainExpiry),
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub(crate) struct DecisionEnvelope {
    pub(crate) contract_version: u16,
    pub(crate) run_id: RunId,
    pub(crate) operation_id: OperationId,
    pub(crate) workflow_sha256: String,
    pub(crate) stage_sha256: String,
    pub(crate) attempt_id: Option<AttemptId>,
    pub(crate) generation: i64,
    pub(crate) before_revision: i64,
    pub(crate) evaluated_at: DateTime<Utc>,
    pub(crate) resolution: Option<NativeDecision>,
    #[serde(flatten)]
    pub(crate) detail: Detail,
}

closed!(Receipt {
    contract_version: u16, action: String, dispatch_event_id: EventId, request_sha256: String,
    receipt_kind: String, #[serde(deserialize_with="required_nullable")] upid: Option<String>,
    accepted_at: DateTime<Utc>,
});

impl DecisionEnvelope {
    pub(crate) fn decode(text: &str) -> Result<Self, Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Root {
            contract_version: u16,
            run_id: RunId,
            operation_id: OperationId,
            workflow_sha256: String,
            stage_sha256: String,
            #[serde(deserialize_with = "required_nullable")]
            attempt_id: Option<AttemptId>,
            generation: i64,
            before_revision: i64,
            evaluated_at: DateTime<Utc>,
            #[serde(deserialize_with = "required_nullable")]
            resolution: Option<NativeDecision>,
            action: String,
            detail: Value,
        }
        let value = unique_object(text, DECISION_LIMIT)?;
        let root: Root = serde_json::from_value(value.clone())?;
        let detail: Detail =
            serde_json::from_value(serde_json::json!({"action":root.action,"detail":root.detail}))?;
        let decoded = Self {
            contract_version: root.contract_version,
            run_id: root.run_id,
            operation_id: root.operation_id,
            workflow_sha256: root.workflow_sha256,
            stage_sha256: root.stage_sha256,
            attempt_id: root.attempt_id,
            generation: root.generation,
            before_revision: root.before_revision,
            evaluated_at: root.evaluated_at,
            resolution: root.resolution,
            detail,
        };
        // Exact roundtrip also excludes serde's sequence form for nested structs
        // and object representations of enum strings inherited from older types.
        require(serde_json::to_value(&decoded)? == value)?;
        decoded.validate()?;
        Ok(decoded)
    }
    pub(crate) fn action(&self) -> &'static str {
        match self.detail {
            Detail::StageActivated(_) => "stage_activated",
            Detail::LeaseAcquired(_) => "lease_acquired",
            Detail::EvaluationStarted(_) => "evaluation_started",
            Detail::LeaseRenewed(_) => "lease_renewed",
            Detail::PveDispatchCommitted(_) => "pve_dispatch_committed",
            Detail::PveEvaluated(_) => "pve_evaluated",
            Detail::FixturePeRegistered(_) => "fixture_pe_registered",
            Detail::FixturePeCompleted(_) => "fixture_pe_completed",
            Detail::FixtureGraceWaiting(_) => "fixture_grace_waiting",
            Detail::LeaseReclaimedSameAttempt(_) => "lease_reclaimed_same_attempt",
            Detail::CredentialDeliveryReclaimed(_) => "credential_delivery_reclaimed",
            Detail::EvaluationReparked(_) => "evaluation_reparked",
            Detail::ScopeExpiredBeforeActivation(_) => "scope_expired_before_activation",
            Detail::ActivatedScopeExpired(_) => "activated_scope_expired",
            Detail::RunCancelled(_) => "run_cancelled",
            Detail::StageCancelledUnexposed(_) => "stage_cancelled_unexposed",
            Detail::StageCancelledExposed(_) => "stage_cancelled_exposed",
            Detail::ResidualLeaseRevoked(_) => "residual_lease_revoked",
            Detail::ReconciliationScheduled(_) => "reconciliation_scheduled",
            Detail::LeaseExpiredUncertain(_) => "lease_expired_uncertain",
        }
    }
    pub(crate) fn validate(&self) -> Result<(), Error> {
        use Detail::*;
        require(
            self.contract_version == 1
                && self.generation > 0
                && self.before_revision >= 0
                && self.before_revision < i64::MAX
                && hash_valid(&self.workflow_sha256)
                && hash_valid(&self.stage_sha256),
        )?;
        require(
            self.attempt_id.is_some()
                || matches!(
                    self.detail,
                    ScopeExpiredBeforeActivation(_) | StageCancelledUnexposed(_) | RunCancelled(_)
                ),
        )?;
        let expected =
            match &self.detail {
                FixturePeRegistered(d) => {
                    require(
                        hash_valid(&d.identity_sha256)
                            && hash_valid(&d.package_sha256)
                            && self.evaluated_at < d.registration_deadline,
                    )?;
                    Some(NativeDecision::Satisfied)
                }
                FixturePeCompleted(d) => {
                    require(
                        hash_valid(&d.report_sha256)
                            && hash_valid(&d.definition_sha256)
                            && self.evaluated_at < d.completion_deadline,
                    )?;
                    Some(if d.succeeded {
                        NativeDecision::Satisfied
                    } else {
                        NativeDecision::Failed
                    })
                }
                FixtureGraceWaiting(d) => {
                    require(self.evaluated_at < d.deadline_at)?;
                    Some(NativeDecision::Waiting)
                }
                StageActivated(d) => {
                    require(
                        (1..=86400).contains(&d.budget_seconds)
                            && d.opened_at.checked_add_signed(chrono::Duration::seconds(
                                i64::from(d.budget_seconds),
                            )) == Some(d.deadline_at)
                            && d.predecessor_operation_id.is_some()
                                == d.predecessor_decision_event_id.is_some(),
                    )?;
                    None
                }
                LeaseAcquired(d) => {
                    require(
                        hash_valid(&d.token_sha256)
                            && !d.worker_id.trim().is_empty()
                            && d.acquired_at < d.expires_at
                            && d.expires_at <= d.deadline_at
                            && d.acquired_at
                                .checked_add_signed(chrono::Duration::seconds(30))
                                .map(|t| t.min(d.deadline_at))
                                == Some(d.expires_at)
                            && (d.purpose == Purpose::ResumeEvaluation)
                                == d.prior_schedule_event_id.is_some(),
                    )?;
                    None
                }
                EvaluationStarted(_) => None,
                LeaseRenewed(d) => {
                    require(
                        d.heartbeat_at < d.expires_at
                            && d.expires_at <= d.deadline_at
                            && d.heartbeat_at
                                .checked_add_signed(chrono::Duration::seconds(30))
                                .map(|t| t.min(d.deadline_at))
                                == Some(d.expires_at),
                    )?;
                    None
                }
                PveDispatchCommitted(d) => {
                    require(hash_valid(&d.pve_plan_sha256) && hash_valid(&d.request_sha256))?;
                    Some(NativeDecision::Ready)
                }
                PveEvaluated(d) => {
                    require(
                        hash_valid(&d.evidence_sha256)
                            && d.advice != NativeDecision::Ready
                            && (d.mode == Mode::Reconciliation)
                                == d.lease_acquisition_event_id.is_none()
                            && !(d.mode == Mode::Preflight && d.advice == NativeDecision::Waiting)
                            && reason_matches(d.advice, d.reason, d.mode),
                    )?;
                    let resolution =
                        if d.mode == Mode::Reconciliation && d.advice == NativeDecision::Waiting {
                            NativeDecision::Unknown
                        } else {
                            d.advice
                        };
                    require(d.schedule.is_some() == (resolution == NativeDecision::Waiting))?;
                    if let Some(s) = &d.schedule {
                        s.validate(ScheduleMode::Waiting, self.evaluated_at, d.deadline_at)?;
                    }
                    Some(resolution)
                }
                LeaseReclaimedSameAttempt(d) => {
                    require(d.reason == Reason::LeaseExpiredBeforeStart)?;
                    None
                }
                CredentialDeliveryReclaimed(d) => {
                    require(
                        d.reason == Reason::LeaseExpiredBeforeCredentialAck
                            && d.scope_key == Scope::MutationStartPe,
                    )?;
                    None
                }
                EvaluationReparked(d) => {
                    require(d.reason == Reason::ReadOnlyEvaluatorLeaseExpired)?;
                    d.schedule
                        .validate(ScheduleMode::Waiting, self.evaluated_at, d.deadline_at)?;
                    Some(NativeDecision::Waiting)
                }
                ScopeExpiredBeforeActivation(d) => {
                    require(
                        self.attempt_id.is_none()
                            && d.reason == Reason::InheritedScopeDeadlineExpired
                            && matches!(
                                d.scope_key,
                                Scope::PeRegistration
                                    | Scope::PeCompletion
                                    | Scope::ShutdownGrace
                                    | Scope::FullOs
                            )
                            && self.evaluated_at >= d.deadline_at,
                    )?;
                    Some(NativeDecision::Unknown)
                }
                ActivatedScopeExpired(d) => {
                    let grace = d.scope_key == Scope::ShutdownGrace;
                    require(
                        self.evaluated_at >= d.deadline_at
                            && d.pe_complete_operation_id.is_some() == grace
                            && d.pe_complete_decision_event_id.is_some() == grace
                            && d.reason
                                == if grace {
                                    Reason::ShutdownGraceDeadlineExpired
                                } else {
                                    Reason::PhaseDeadlineExpired
                                },
                    )?;
                    Some(NativeDecision::Unknown)
                }
                RunCancelled(d) => {
                    require(d.reason == Reason::RunCancellationRequested)?;
                    None
                }
                StageCancelledUnexposed(d) => {
                    require(
                        d.reason == Reason::RunCancelledBeforeExposure
                            && d.scope_key.is_some() == d.deadline_at.is_some()
                            && (self.attempt_id.is_none() || d.scope_key.is_some()),
                    )?;
                    Some(NativeDecision::Blocked)
                }
                StageCancelledExposed(d) => {
                    require(d.reason == Reason::RunCancelledOutcomeUncertain)?;
                    Some(NativeDecision::Unknown)
                }
                ResidualLeaseRevoked(d) => {
                    require(d.reason == Reason::TerminalLeaseRevoked)?;
                    None
                }
                ReconciliationScheduled(d) => {
                    require(matches!(
                        d.reason,
                        Reason::OriginalDispatchUnresolved
                            | Reason::OriginalReceiptCaptured
                            | Reason::ObservationUnavailable
                            | Reason::ReconciliationStillUnknown
                    ))?;
                    d.schedule.validate(
                        ScheduleMode::UnknownReconciliation,
                        self.evaluated_at,
                        d.deadline_at,
                    )?;
                    None
                }
                LeaseExpiredUncertain(d) => {
                    require(d.reason == Reason::LeaseExpiredAfterDispatch)?;
                    Some(NativeDecision::Unknown)
                }
            };
        require(self.resolution == expected)
    }
}

impl Schedule {
    pub(crate) fn validate(
        &self,
        mode: ScheduleMode,
        at: DateTime<Utc>,
        deadline: DateTime<Utc>,
    ) -> Result<(), Error> {
        require(
            self.mode == mode
                && self.unavailable_count <= 4
                && at < deadline
                && (mode != ScheduleMode::Waiting || self.next_check_at.is_some())
                && self.next_check_at.is_none_or(|n| n >= at && n <= deadline),
        )
    }
}

fn reason_matches(advice: NativeDecision, reason: ProvisioningReasonV1, mode: Mode) -> bool {
    use NativeDecision::*;
    use ProvisioningReasonV1::*;
    match reason {
        BindingMismatch
        | ReceiptMismatch
        | TemplateMismatch
        | TargetOccupied
        | BeforeStateChanged
        | VmNotStoppedUnlocked
        | InventoryContradiction
        | IdentityCollision
        | ProvenanceMismatch => advice == Conflicted,
        Unauthorized
        | UnsupportedInfrastructure
        | InsufficientCapacity
        | MediaMissing
        | UnsupportedLayout => advice == Blocked,
        TaskRunning => advice == Waiting && mode != Mode::Preflight,
        TaskFailed => matches!(advice, Failed | Unknown) && mode != Mode::Preflight,
        CloneSatisfied
        | CapacitySatisfied
        | ConfigurePeSatisfied
        | StartPeSatisfied
        | StopSatisfied
        | ConfigureDiskSatisfied
        | StartDiskSatisfied => advice == Satisfied && mode != Mode::Preflight,
        ObservedNoChange => advice == Satisfied && mode == Mode::Preflight,
        CloneReady | CapacityReady | ConfigurePeReady | StartPeReady | StopReady
        | ConfigureDiskReady | StartDiskReady => false,
        StateNotEligible
        | Cancelled
        | ObservationMissing
        | ObservationUnavailable
        | ObservationNotFresh
        | IncompleteIdentityCoverage
        | MediaCoverageIncomplete
        | OwnershipMissing
        | PredecessorMismatch
        | DispatchAlreadyPossible
        | DispatchProvenanceMissing
        | ReceiptMissing
        | PostconditionPending
        | DeadlineExpired
        | PowerMismatch => advice == Unknown,
        // Closed vocabulary values unused by the completed evaluator cannot
        // be invented as proof of a result that evaluator never emits.
        ProvenanceMissing | BoundIdentityChanged | CapacityMismatch | ProfileMismatch => false,
    }
}

pub(crate) fn require(ok: bool) -> Result<(), Error> {
    if ok { Ok(()) } else { Err(Error::Validation) }
}
pub(crate) fn hash_valid(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub(crate) fn canonical<T: Serialize>(v: &T) -> Result<String, Error> {
    String::from_utf8(
        event_journal::canonical_json_bytes(&serde_json::to_value(v)?)
            .map_err(|_| Error::Validation)?,
    )
    .map_err(|_| Error::Validation)
}
pub(crate) fn digest<T: Serialize>(v: &T) -> Result<String, Error> {
    event_journal::payload_digest(&serde_json::to_value(v)?).map_err(|_| Error::Validation)
}
pub(crate) fn decode_exact<T: de::DeserializeOwned + Serialize>(
    text: &str,
    limit: usize,
) -> Result<T, Error> {
    let value = unique_object(text, limit)?;
    let result: T = serde_json::from_value(value.clone())?;
    require(serde_json::to_value(&result)? == value)?;
    Ok(result)
}
pub(crate) fn unique_object(text: &str, limit: usize) -> Result<Value, Error> {
    require(text.len() <= limit)?;
    let v = serde_json::from_str::<Unique>(text)?.0;
    require(v.is_object())?;
    Ok(v)
}
// Recursive duplicate checking must precede buffering heterogeneous detail and
// legacy nested provisioning values into serde_json::Value.
struct Unique(Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct Visitor;
        impl<'de> de::Visitor<'de> for Visitor {
            type Value = Unique;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("unique JSON value")
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Unique, E> {
                serde_json::Number::from_f64(v)
                    .map(|n| Unique(Value::Number(n)))
                    .ok_or_else(|| E::custom("invalid number"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
            fn visit_seq<A: de::SeqAccess<'de>>(self, mut a: A) -> Result<Unique, A::Error> {
                let mut out = Vec::new();
                while let Some(Unique(v)) = a.next_element()? {
                    out.push(v)
                }
                Ok(Unique(out.into()))
            }
            fn visit_map<A: de::MapAccess<'de>>(self, mut a: A) -> Result<Unique, A::Error> {
                let mut out = serde_json::Map::new();
                while let Some((k, Unique(v))) = a.next_entry::<String, Unique>()? {
                    if out.insert(k, v).is_some() {
                        return Err(de::Error::custom("duplicate field"));
                    }
                }
                Ok(Unique(out.into()))
            }
        }
        d.deserialize_any(Visitor)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn sample() -> Value {
        json!({"contract_version":1,"action":"stage_activated","run_id":RunId::new(),
        "operation_id":OperationId::new(),"workflow_sha256":"a".repeat(64),"stage_sha256":"b".repeat(64),
        "attempt_id":AttemptId::new(),"generation":1,"before_revision":0,"evaluated_at":"2026-09-05T12:00:00Z",
        "resolution":null,"detail":{"scope_key":"mutation_clone","anchor_operation_id":OperationId::new(),
        "anchor_event_id":EventId::new(),"opened_at":"2026-09-05T12:00:00Z","budget_seconds":300,
        "deadline_at":"2026-09-05T12:05:00Z","predecessor_operation_id":null,"predecessor_decision_event_id":null}})
    }
    fn decode(v: &Value) -> Result<DecisionEnvelope, Error> {
        DecisionEnvelope::decode(&v.to_string())
    }
    #[test]
    fn closed_decision_accepts_required_nulls_and_rejects_missing_and_unknown_fields() {
        let good = sample();
        assert!(decode(&good).is_ok());
        for field in ["attempt_id", "resolution"] {
            let mut v = good.clone();
            v.as_object_mut().unwrap().remove(field);
            assert!(decode(&v).is_err(), "{field}");
        }
        for field in ["predecessor_operation_id", "predecessor_decision_event_id"] {
            let mut v = good.clone();
            v["detail"].as_object_mut().unwrap().remove(field);
            assert!(decode(&v).is_err(), "{field}");
        }
        for field in ["root", "detail"] {
            let mut v = good.clone();
            if field == "root" {
                v["unknown"] = json!(1)
            } else {
                v["detail"]["unknown"] = json!(1)
            }
            assert!(decode(&v).is_err());
        }
        for field in [
            "resolution",
            "attempt_id",
            "workflow_sha256",
            "generation",
            "detail",
        ] {
            let mut v = good.clone();
            v[field] = json!([]);
            assert!(decode(&v).is_err());
        }
        let mut v = good.clone();
        v["workflow_sha256"] = json!("A".repeat(64));
        assert!(decode(&v).is_err());
        v = good.clone();
        v["resolution"] = json!("satisfied");
        assert!(decode(&v).is_err());
        v = good.clone();
        v["attempt_id"] = Value::Null;
        assert!(decode(&v).is_err());
        let text = good.to_string();
        assert!(
            DecisionEnvelope::decode(&text.replacen("{", "{\"contract_version\":1,", 1)).is_err()
        );
        assert!(
            DecisionEnvelope::decode(&text.replace(
                "\"scope_key\":",
                "\"scope_key\":\"mutation_clone\",\"scope_key\":"
            ))
            .is_err()
        );
        assert!(DecisionEnvelope::decode("[]").is_err());
    }

    fn actions() -> Vec<Value> {
        let event = EventId::new();
        let op = OperationId::new();
        let at = "2026-09-05T12:00:00Z";
        let deadline = "2026-09-05T12:05:00Z";
        let schedule =
            json!({"mode":"waiting","next_check_at":"2026-09-05T12:00:02Z","unavailable_count":0});
        let mut values = vec![sample()];
        for (action, resolution, detail) in [
            (
                "lease_acquired",
                Value::Null,
                json!({"purpose":"initial_evaluation","acquisition_event_id":event,"token_sha256":"a".repeat(64),"worker_id":"worker","acquired_at":at,"expires_at":"2026-09-05T12:00:30Z","deadline_at":deadline,"prior_schedule_event_id":null}),
            ),
            (
                "evaluation_started",
                Value::Null,
                json!({"lease_acquisition_event_id":event,"activity":"preflight_read"}),
            ),
            (
                "lease_renewed",
                Value::Null,
                json!({"lease_acquisition_event_id":event,"heartbeat_at":at,"expires_at":"2026-09-05T12:00:30Z","deadline_at":deadline}),
            ),
            (
                "pve_dispatch_committed",
                json!("ready"),
                json!({"preflight_event_id":event,"pve_plan_sha256":"a".repeat(64),"request_sha256":"b".repeat(64),"dispatched_at":at,"lease_acquisition_event_id":event}),
            ),
            (
                "pve_evaluated",
                json!("waiting"),
                json!({"mode":"outcome","advice":"waiting","evidence_event_id":event,"evidence_sha256":"a".repeat(64),"scope_key":"mutation_clone","deadline_at":deadline,"reason":"task_running","lease_acquisition_event_id":event,"schedule":schedule}),
            ),
            (
                "lease_reclaimed_same_attempt",
                Value::Null,
                json!({"lease_acquisition_event_id":event,"scope_key":"mutation_clone","deadline_at":deadline,"reason":"lease_expired_before_start"}),
            ),
            (
                "evaluation_reparked",
                json!("waiting"),
                json!({"lease_acquisition_event_id":event,"activity_event_id":event,"scope_key":"mutation_clone","deadline_at":deadline,"reason":"read_only_evaluator_lease_expired","schedule":schedule}),
            ),
            (
                "scope_expired_before_activation",
                json!("unknown"),
                json!({"scope_key":"pe_registration","anchor_operation_id":op,"anchor_event_id":event,"deadline_at":at,"reason":"inherited_scope_deadline_expired"}),
            ),
            (
                "activated_scope_expired",
                json!("unknown"),
                json!({"scope_key":"mutation_clone","anchor_operation_id":op,"anchor_event_id":event,"deadline_at":at,"pe_complete_operation_id":null,"pe_complete_decision_event_id":null,"reason":"phase_deadline_expired"}),
            ),
            (
                "run_cancelled",
                Value::Null,
                json!({"reason":"run_cancellation_requested"}),
            ),
            (
                "stage_cancelled_unexposed",
                json!("blocked"),
                json!({"cancellation_event_id":event,"scope_key":null,"deadline_at":null,"reason":"run_cancelled_before_exposure"}),
            ),
            (
                "stage_cancelled_exposed",
                json!("unknown"),
                json!({"cancellation_event_id":event,"scope_key":"mutation_clone","deadline_at":deadline,"dispatch_event_id":event,"reason":"run_cancelled_outcome_uncertain"}),
            ),
            (
                "residual_lease_revoked",
                Value::Null,
                json!({"terminal_decision_event_id":event,"lease_acquisition_event_id":event,"reason":"terminal_lease_revoked"}),
            ),
            (
                "reconciliation_scheduled",
                Value::Null,
                json!({"terminal_decision_event_id":event,"dispatch_event_id":event,"scope_key":"mutation_clone","deadline_at":deadline,"reason":"original_dispatch_unresolved","schedule":{"mode":"unknown_reconciliation","next_check_at":null,"unavailable_count":0}}),
            ),
            (
                "lease_expired_uncertain",
                json!("unknown"),
                json!({"lease_acquisition_event_id":event,"dispatch_event_id":event,"scope_key":"mutation_clone","deadline_at":deadline,"reason":"lease_expired_after_dispatch"}),
            ),
        ] {
            let mut v = sample();
            v["action"] = json!(action);
            v["resolution"] = resolution;
            v["detail"] = detail;
            if matches!(
                action,
                "scope_expired_before_activation" | "stage_cancelled_unexposed" | "run_cancelled"
            ) {
                v["attempt_id"] = Value::Null;
            }
            values.push(v);
        }
        values
    }
    #[test]
    fn every_initial_action_is_closed_and_pairs_its_resolution_and_reason() {
        for good in actions() {
            assert!(decode(&good).is_ok(), "{}", good["action"]);
            let mut v = good.clone();
            v["detail"]["extra"] = json!(0);
            assert!(decode(&v).is_err());
            v = good.clone();
            v["resolution"] = json!("conflicted");
            assert!(decode(&v).is_err());
            for field in good["detail"].as_object().unwrap().keys() {
                let mut v = good.clone();
                v["detail"].as_object_mut().unwrap().remove(field);
                assert!(decode(&v).is_err(), "missing {}.{field}", good["action"]);
            }
            if good["detail"].get("reason").is_some() {
                let mut v = good.clone();
                v["detail"]["reason"] = json!("unrecognized_reason");
                assert!(decode(&v).is_err());
            }
            let encoded = good.to_string();
            let key = good["detail"].as_object().unwrap().keys().next().unwrap();
            let needle = format!("\"{key}\":");
            let duplicate = format!("\"{key}\":null,\"{key}\":");
            assert!(DecisionEnvelope::decode(&encoded.replacen(&needle, &duplicate, 1)).is_err());
        }
        let mut future = sample();
        future["action"] = json!("grace_wait_activated");
        assert!(decode(&future).is_err());
    }
    #[test]
    fn reconciliation_advice_never_reopens_waiting_and_nullable_schedule_is_exact() {
        let good = actions()
            .into_iter()
            .find(|v| v["action"] == "pve_evaluated")
            .unwrap();
        let mut v = good.clone();
        v["detail"]["mode"] = json!("reconciliation");
        v["detail"]["lease_acquisition_event_id"] = Value::Null;
        v["detail"]["schedule"] = Value::Null;
        v["resolution"] = json!("unknown");
        assert!(decode(&v).is_ok());
        v["resolution"] = json!("waiting");
        assert!(decode(&v).is_err());
        v = good.clone();
        v["detail"]["mode"] = json!("preflight");
        assert!(decode(&v).is_err());
        v = good.clone();
        v["detail"]["schedule"]["next_check_at"] = Value::Null;
        assert!(decode(&v).is_err());
        v = good.clone();
        v["detail"]["schedule"]["unavailable_count"] = json!(5);
        assert!(decode(&v).is_err());
        v = good.clone();
        v["detail"]["schedule"]["next_check_at"] = json!("2026-09-05T12:05:01Z");
        assert!(decode(&v).is_err());
        v = good.clone();
        v["detail"]["schedule"]["extra"] = json!(1);
        assert!(decode(&v).is_err());
        v = good.clone();
        v["detail"]["schedule"]
            .as_object_mut()
            .unwrap()
            .remove("next_check_at");
        assert!(decode(&v).is_err());
    }
    #[test]
    fn decision_and_evidence_limits_count_exact_input_octets() {
        let mut v = actions()
            .into_iter()
            .find(|v| v["action"] == "lease_acquired")
            .unwrap();
        let size = v.to_string().len();
        v["detail"]["worker_id"] = json!("x".repeat(DECISION_LIMIT - size + 6));
        assert_eq!(v.to_string().len(), DECISION_LIMIT);
        assert!(decode(&v).is_ok());
        v["detail"]["worker_id"] =
            json!(format!("{}x", v["detail"]["worker_id"].as_str().unwrap()));
        assert!(decode(&v).is_err());
        let object = format!("{{\"value\":\"{}\"}}", "x".repeat(EVIDENCE_LIMIT - 12));
        assert_eq!(object.len(), EVIDENCE_LIMIT);
        assert!(unique_object(&object, EVIDENCE_LIMIT).is_ok());
        assert!(unique_object(&(object + " "), EVIDENCE_LIMIT).is_err());
        assert!(unique_object("{\"x\":{\"a\":1,\"a\":2}}", EVIDENCE_LIMIT).is_err());
    }
    #[test]
    fn receipt_requires_null_and_exact_closed_original_identity_fields() {
        let good = json!({"contract_version":1,"action":"pve_receipt_captured","dispatch_event_id":EventId::new(),
            "request_sha256":"a".repeat(64),"receipt_kind":"synchronous","upid":null,"accepted_at":"2026-09-05T12:00:00Z"});
        assert!(decode_exact::<Receipt>(&good.to_string(), DECISION_LIMIT).is_ok());
        let mut v = good.clone();
        v.as_object_mut().unwrap().remove("upid");
        assert!(decode_exact::<Receipt>(&v.to_string(), DECISION_LIMIT).is_err());
        let mut v = good.clone();
        v["extra"] = json!(1);
        assert!(decode_exact::<Receipt>(&v.to_string(), DECISION_LIMIT).is_err());
        assert!(decode_exact::<Receipt>("[]", DECISION_LIMIT).is_err());
    }

    #[test]
    fn all_emitted_evaluator_and_control_reasons_have_closed_pairings() {
        let base = actions()
            .into_iter()
            .find(|v| v["action"] == "pve_evaluated")
            .unwrap();
        for (reason, advice, mode) in [
            ("binding_mismatch", "conflicted", "outcome"),
            ("receipt_mismatch", "conflicted", "outcome"),
            ("template_mismatch", "conflicted", "preflight"),
            ("target_occupied", "conflicted", "preflight"),
            ("before_state_changed", "conflicted", "outcome"),
            ("vm_not_stopped_unlocked", "conflicted", "preflight"),
            ("inventory_contradiction", "conflicted", "outcome"),
            ("identity_collision", "conflicted", "outcome"),
            ("provenance_mismatch", "conflicted", "outcome"),
            ("unauthorized", "blocked", "preflight"),
            ("unsupported_infrastructure", "blocked", "preflight"),
            ("insufficient_capacity", "blocked", "preflight"),
            ("media_missing", "blocked", "preflight"),
            ("unsupported_layout", "blocked", "preflight"),
            ("state_not_eligible", "unknown", "outcome"),
            ("cancelled", "unknown", "outcome"),
            ("observation_missing", "unknown", "preflight"),
            ("observation_unavailable", "unknown", "preflight"),
            ("observation_not_fresh", "unknown", "preflight"),
            ("incomplete_identity_coverage", "unknown", "preflight"),
            ("media_coverage_incomplete", "unknown", "preflight"),
            ("ownership_missing", "unknown", "preflight"),
            ("predecessor_mismatch", "unknown", "preflight"),
            ("dispatch_already_possible", "unknown", "preflight"),
            ("dispatch_provenance_missing", "unknown", "outcome"),
            ("receipt_missing", "unknown", "outcome"),
            ("postcondition_pending", "unknown", "outcome"),
            ("deadline_expired", "unknown", "outcome"),
            ("power_mismatch", "unknown", "preflight"),
            ("task_failed", "failed", "outcome"),
            ("task_failed", "unknown", "outcome"),
            ("clone_satisfied", "satisfied", "outcome"),
            ("capacity_satisfied", "satisfied", "outcome"),
            ("configure_pe_satisfied", "satisfied", "outcome"),
            ("start_pe_satisfied", "satisfied", "outcome"),
            ("stop_satisfied", "satisfied", "outcome"),
            ("configure_disk_satisfied", "satisfied", "outcome"),
            ("start_disk_satisfied", "satisfied", "outcome"),
            ("observed_no_change", "satisfied", "preflight"),
        ] {
            let mut v = base.clone();
            v["detail"]["reason"] = json!(reason);
            v["detail"]["advice"] = json!(advice);
            v["detail"]["mode"] = json!(mode);
            v["resolution"] = json!(advice);
            v["detail"]["schedule"] = Value::Null;
            assert!(decode(&v).is_ok(), "{reason}/{advice}/{mode}");
            v["detail"]["advice"] = json!("waiting");
            v["resolution"] = json!("waiting");
            assert!(decode(&v).is_err(), "wrong advice for {reason}");
        }
        for reason in [
            "binding_mismatch",
            "provenance_missing",
            "bound_identity_changed",
            "capacity_mismatch",
            "profile_mismatch",
            "clone_ready",
            "capacity_ready",
            "configure_pe_ready",
            "start_pe_ready",
            "stop_ready",
            "configure_disk_ready",
            "start_disk_ready",
        ] {
            let mut v = base.clone();
            v["detail"]["reason"] = json!(reason);
            assert!(decode(&v).is_err());
        }
        let schedule = actions()
            .into_iter()
            .find(|v| v["action"] == "reconciliation_scheduled")
            .unwrap();
        for reason in [
            "original_dispatch_unresolved",
            "original_receipt_captured",
            "observation_unavailable",
            "reconciliation_still_unknown",
        ] {
            let mut v = schedule.clone();
            v["detail"]["reason"] = json!(reason);
            assert!(decode(&v).is_ok());
        }
        let mut grace = actions()
            .into_iter()
            .find(|v| v["action"] == "activated_scope_expired")
            .unwrap();
        grace["detail"]["scope_key"] = json!("shutdown_grace");
        grace["detail"]["reason"] = json!("shutdown_grace_deadline_expired");
        assert!(decode(&grace).is_err());
        grace["detail"]["pe_complete_operation_id"] = json!(OperationId::new());
        assert!(decode(&grace).is_err());
        grace["detail"]["pe_complete_decision_event_id"] = json!(EventId::new());
        assert!(decode(&grace).is_ok());
    }
}
