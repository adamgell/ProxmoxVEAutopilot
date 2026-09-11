use super::*;
pub(super) fn evaluate(
    context: &ProvisioningEvaluationContextV1,
    evidence: &ProvisioningEvidenceV1,
    now: DateTime<Utc>,
) -> Check<ProvisioningEvaluationV1> {
    use ProvisioningActionV1::*;
    use ProvisioningReasonV1::*;
    let (c, e) = common(context, evidence, now, false)?;
    let ProvisioningDispatchStateV1::Recorded { dispatch, receipt } = &c.dispatch else {
        return Err(unknown(DispatchProvenanceMissing));
    };
    let action = c.plan.action();
    let task_bearing = !matches!(action, ConfigurePe | ConfigureDisk);
    if receipt
        .as_ref()
        .is_some_and(|r| r.accepted_at() > now || r.accepted_at() > e.collected_at)
    {
        return Err(unknown(ObservationNotFresh));
    }
    let min = Some(dispatch.dispatched_at());
    let before = dispatch.request().expected_before();
    if action == Clone {
        let source = read(
            &e.source_config,
            c,
            e,
            now,
            min,
            ProvisioningVmConfigV1::observed_at,
        )?;
        let source_power = read(&e.source_power, c, e, now, min, VmPowerStatus::observed_at)?;
        if !physical::semantic_eq(before.config(), source)
            || !physical::template(source, source_power, &c.plan)
        {
            return Err(conflict(TemplateMismatch));
        }
        if let Some(target) = &e.target_config
            && target.result == Err(PveReadError::NotFound)
        {
            clock(target.observed_at, target.observed_at, c, e, now, min)?;
            identities(c, e, now, min, false)?;
            let receipt = receipt.as_ref().ok_or_else(|| unknown(ReceiptMissing))?;
            let task = read(
                &e.task,
                c,
                e,
                now,
                Some(receipt.accepted_at()),
                crate::TaskStatus::observed_at,
            )?;
            return Ok(match task.state() {
                TaskState::Running => result(NativeDecision::Waiting, TaskRunning),
                TaskState::CompleteFailure => result(NativeDecision::Failed, TaskFailed),
                TaskState::CompleteSuccess => unknown(PostconditionPending),
            });
        }
    }
    let target = read(
        &e.target_config,
        c,
        e,
        now,
        min,
        ProvisioningVmConfigV1::observed_at,
    )?;
    let power = read(&e.target_power, c, e, now, min, VmPowerStatus::observed_at)?;
    if !physical::owned(target, &c.clone_request) {
        return Err(conflict(ProvenanceMismatch));
    }
    identities(c, e, now, min, true)?;
    let unchanged = physical::semantic_eq(before.config(), target)
        && before.power().power() == power.power()
        && before.power().locked() == power.locked();
    let desired_config = if action == Clone {
        physical::clone_after(before.config(), target, &c.clone_request)
    } else {
        physical::after(&c.plan, before.config(), target)
    };
    if !desired_config && !unchanged {
        return Err(conflict(BeforeStateChanged));
    }
    if !physical::power_valid(power) {
        return Err(conflict(VmNotStoppedUnlocked));
    }
    if receipt.is_none() && (task_bearing || c.mode != ProvisioningEvaluationModeV1::Reconciliation)
    {
        return Err(unknown(ReceiptMissing));
    }
    let desired_power = match action {
        StartPe | StartDisk => PowerState::Running,
        _ => PowerState::Stopped,
    };
    if task_bearing {
        let receipt = receipt.as_ref().expect("task receipt checked");
        let task = read(
            &e.task,
            c,
            e,
            now,
            Some(receipt.accepted_at()),
            crate::TaskStatus::observed_at,
        )?;
        if receipt.receipt() != &MutationReceipt::Task(task.upid().clone()) {
            return Err(conflict(ReceiptMismatch));
        }
        match task.state() {
            TaskState::Running => return Ok(result(NativeDecision::Waiting, TaskRunning)),
            TaskState::CompleteFailure => {
                return Ok(if unchanged {
                    result(NativeDecision::Failed, TaskFailed)
                } else {
                    unknown(TaskFailed)
                });
            }
            TaskState::CompleteSuccess => {}
        }
    }
    if !desired_config || power.power() != desired_power {
        return Ok(unknown(PostconditionPending));
    }
    Ok(result(
        NativeDecision::Satisfied,
        match action {
            Clone => CloneSatisfied,
            EnsureCapacity => CapacitySatisfied,
            ConfigurePe => ConfigurePeSatisfied,
            StartPe => StartPeSatisfied,
            EnsureStopped => StopSatisfied,
            ConfigureDisk => ConfigureDiskSatisfied,
            StartDisk => StartDiskSatisfied,
        },
    ))
}
