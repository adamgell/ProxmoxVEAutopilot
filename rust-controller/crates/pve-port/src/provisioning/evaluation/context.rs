use super::*;
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvisioningEvaluationModeV1 {
    Preflight,
    Outcome,
    Reconciliation,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
// Keep the accepted explicit input API; this inspection value is not a hot
// transport queue, and boxing would change its public construction contract.
#[allow(clippy::large_enum_variant)]
pub enum ProvisioningDispatchStateV1 {
    NotDispatched,
    Recorded {
        dispatch: ProvisioningDispatchV1,
        receipt: Option<ProvisioningReceiptV1>,
    },
    PossibleWithoutRecord,
}
#[derive(Clone, Debug, Serialize)]
pub struct ProvisioningEvaluationContextInputV1 {
    pub binding: ProvisioningBindingV1,
    pub plan: ProvisioningOperationPlanV1,
    pub source: NativeEvidenceSource,
    pub state: ExecutionState,
    pub mode: ProvisioningEvaluationModeV1,
    pub cancelled: bool,
    pub mutation_deadline: DateTime<Utc>,
    pub freshness_seconds: u16,
    pub dispatch: ProvisioningDispatchStateV1,
    pub clone_request: CloneRequest,
    pub clone_ownership: Option<ProvisioningCloneOwnershipV1>,
    pub predecessor: Option<ProvisioningStageBaselineV1>,
}
#[derive(Clone, Debug, Serialize)]
pub struct ProvisioningEvaluationContextV1 {
    input: ProvisioningEvaluationContextInputV1,
}
impl ProvisioningEvaluationContextV1 {
    pub fn new(i: ProvisioningEvaluationContextInputV1) -> Result<Self, InvalidProvisioning> {
        if !(1..=300).contains(&i.freshness_seconds)
            || !i.binding.validates(&i.plan)
            || i.clone_request.vm() != i.plan.expected().vm()
        {
            return Err(InvalidProvisioning);
        }
        if i.plan.action() == ProvisioningActionV1::Clone {
            if i.clone_request.operation_id() != i.binding.operation_id()
                || i.clone_ownership.is_some()
                || i.predecessor.is_some()
            {
                return Err(InvalidProvisioning);
            }
        } else {
            let owner = i.clone_ownership.as_ref().ok_or(InvalidProvisioning)?;
            let prior = i.predecessor.as_ref().ok_or(InvalidProvisioning)?;
            if owner.request().clone_request() != &i.clone_request
                || owner.request().plan().expected() != i.plan.expected()
                || !i.binding.same_workflow(owner.request().binding())
                || !i.binding.same_workflow(prior.binding())
                || i.binding.operation_id() == prior.binding().operation_id()
                || i.binding.operation_id() == owner.request().operation_id()
                || prior.plan().expected() != i.plan.expected()
                || physical::predecessor(i.plan.action()) != Some(prior.plan().action())
            {
                return Err(InvalidProvisioning);
            }
        }
        if let ProvisioningDispatchStateV1::Recorded { dispatch, receipt } = &i.dispatch {
            if !dispatch
                .request()
                .binding()
                .same_operation_attempt(&i.binding)
                || dispatch.request().plan() != &i.plan
                || dispatch.source() != i.source
                || receipt.as_ref().is_some_and(|r| r.dispatch() != dispatch)
            {
                return Err(InvalidProvisioning);
            }
            match dispatch.request() {
                ProvisioningMutationRequestV1::Clone(r)
                    if r.clone_request() == &i.clone_request => {}
                ProvisioningMutationRequestV1::Clone(_) => return Err(InvalidProvisioning),
                request => {
                    if !request.matches_history(
                        i.clone_ownership.as_ref().ok_or(InvalidProvisioning)?,
                        i.predecessor.as_ref().ok_or(InvalidProvisioning)?,
                    ) {
                        return Err(InvalidProvisioning);
                    }
                }
            }
        }
        Ok(Self { input: i })
    }
    pub fn facts(&self) -> &ProvisioningEvaluationContextInputV1 {
        &self.input
    }
}
/// Historical physical proof; no raw constructor or persisted decoder.
/// ```compile_fail
/// use pve_port::ProvisioningCloneOwnershipV1;
/// fn unchecked(s:&str) {let _:ProvisioningCloneOwnershipV1=serde_json::from_str(s).unwrap();}
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningCloneOwnershipV1 {
    request: CloneProvisioningRequestV1,
    dispatch: ProvisioningDispatchV1,
    receipt: ProvisioningReceiptV1,
    proof: ProvisioningEvidenceV1,
    mutation_deadline: DateTime<Utc>,
    freshness_seconds: u16,
    bound_at: DateTime<Utc>,
}
impl ProvisioningCloneOwnershipV1 {
    pub fn from_satisfied_clone(
        c: &ProvisioningEvaluationContextV1,
        e: &ProvisioningEvidenceV1,
        as_of: DateTime<Utc>,
    ) -> Result<Self, InvalidProvisioning> {
        if c.facts().plan.action() != ProvisioningActionV1::Clone
            || evaluate_provisioning_outcome(c, e, as_of).decision != NativeDecision::Satisfied
        {
            return Err(InvalidProvisioning);
        }
        let ProvisioningDispatchStateV1::Recorded {
            dispatch,
            receipt: Some(receipt),
        } = &c.facts().dispatch
        else {
            return Err(InvalidProvisioning);
        };
        let ProvisioningMutationRequestV1::Clone(request) = dispatch.request() else {
            return Err(InvalidProvisioning);
        };
        Ok(Self {
            request: request.clone(),
            dispatch: dispatch.clone(),
            receipt: receipt.clone(),
            proof: e.clone(),
            mutation_deadline: c.facts().mutation_deadline,
            freshness_seconds: c.facts().freshness_seconds,
            bound_at: as_of,
        })
    }
    pub fn request(&self) -> &CloneProvisioningRequestV1 {
        &self.request
    }
    pub fn dispatch(&self) -> &ProvisioningDispatchV1 {
        &self.dispatch
    }
    pub fn receipt(&self) -> &ProvisioningReceiptV1 {
        &self.receipt
    }
    pub fn proof(&self) -> &ProvisioningEvidenceV1 {
        &self.proof
    }
    pub fn config(&self) -> &ProvisioningVmConfigV1 {
        self.proof
            .facts()
            .target_config
            .as_ref()
            .expect("satisfied target")
            .result
            .as_ref()
            .expect("satisfied config")
    }
    pub fn mutation_deadline(&self) -> DateTime<Utc> {
        self.mutation_deadline
    }
    pub fn freshness_seconds(&self) -> u16 {
        self.freshness_seconds
    }
    pub fn bound_at(&self) -> DateTime<Utc> {
        self.bound_at
    }
}
/// Verified physical predecessor, never a durable predecessor decision.
/// ```compile_fail
/// use pve_port::ProvisioningStageBaselineV1;
/// fn unchecked(s:&str) {let _:ProvisioningStageBaselineV1=serde_json::from_str(s).unwrap();}
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningStageBaselineV1 {
    binding: ProvisioningBindingV1,
    plan: ProvisioningOperationPlanV1,
    config: ProvisioningVmConfigV1,
    power: VmPowerStatus,
    evidence_sha256: String,
    evaluated_at: DateTime<Utc>,
}
impl ProvisioningStageBaselineV1 {
    pub fn from_satisfied(
        c: &ProvisioningEvaluationContextV1,
        e: &ProvisioningEvidenceV1,
        as_of: DateTime<Utc>,
    ) -> Result<Self, InvalidProvisioning> {
        let r = if c.facts().mode == ProvisioningEvaluationModeV1::Preflight {
            evaluate_provisioning_preflight(c, e, as_of)
        } else {
            evaluate_provisioning_outcome(c, e, as_of)
        };
        if r.decision != NativeDecision::Satisfied {
            return Err(InvalidProvisioning);
        }
        Ok(Self {
            binding: c.facts().binding.clone(),
            plan: c.facts().plan.clone(),
            config: e
                .facts()
                .target_config
                .as_ref()
                .ok_or(InvalidProvisioning)?
                .result
                .clone()
                .map_err(|_| InvalidProvisioning)?,
            power: e
                .facts()
                .target_power
                .as_ref()
                .ok_or(InvalidProvisioning)?
                .result
                .clone()
                .map_err(|_| InvalidProvisioning)?,
            evidence_sha256: fingerprint(e)?,
            evaluated_at: as_of,
        })
    }
    pub fn binding(&self) -> &ProvisioningBindingV1 {
        &self.binding
    }
    pub fn plan(&self) -> &ProvisioningOperationPlanV1 {
        &self.plan
    }
    pub fn config(&self) -> &ProvisioningVmConfigV1 {
        &self.config
    }
    pub fn power(&self) -> &VmPowerStatus {
        &self.power
    }
    pub fn evidence_sha256(&self) -> &str {
        &self.evidence_sha256
    }
    pub fn evaluated_at(&self) -> DateTime<Utc> {
        self.evaluated_at
    }
}
