#[allow(
    dead_code,
    reason = "shared store scenario includes steps for other harnesses"
)]
#[path = "../../postgres-store/tests/osdeploy_execution_support/mod.rs"]
mod osdeploy_execution_support;
#[allow(
    dead_code,
    reason = "shared store fixture includes tests for other harnesses"
)]
#[path = "../../postgres-store/tests/osdeploy_support/mod.rs"]
mod osdeploy_support;

use controller_domain::ExecutionState;
use operation_controller::{OsDeployController, OsDeployControllerError as Error};
use osdeploy_adapter::OsDeployStage as Stage;
use osdeploy_execution_support::Scenario;
use postgres_store::OsDeployProgress as Progress;
use pve_port::{
    FakeControllerCheckpoint, FakeMutationOutcome, ProvisioningActionV1,
    ProvisioningFaultSelectorV1,
};
use std::{sync::Arc, time::Duration};

fn controller(s: &Scenario) -> Arc<OsDeployController> {
    Arc::new(
        OsDeployController::new(s.db.store.clone(), s.db.scheduler(), s.fake.clone(), 1).unwrap(),
    )
}

async fn dispatched_unknown(s: &Scenario) -> postgres_store::LeaseGrant {
    let ready = s.ready(Stage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&ready.grant, ready.revision, ready.event, &ready.request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let vm = ready.request.plan().expected().vm();
    s.fake.enqueue_provisioning_config_read(
        vm.node().clone(),
        vm.target_vmid(),
        pve_port::FakeProvisioningConfigReadV1::Error(pve_port::PveReadError::TransportUnavailable),
    );
    assert_eq!(
        s.observe_and_decide(&ready.grant).await,
        ExecutionState::Unknown
    );
    scheduler
        .repair_osdeploy_schedules(&mut postgres_store::OsDeployRepairCursor::default())
        .await
        .unwrap();
    ready.grant
}

#[tokio::test]
async fn unknown_observation_has_db_budget_without_a_lease_or_writes() {
    let s = Scenario::new(300, true).await;
    let grant = dispatched_unknown(&s).await;
    let snapshot =
        s.db.store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
    let before = s.db.snapshot().await;
    assert!(before["worker_leases"].as_array().unwrap().is_empty());
    let observation =
        s.db.store
            .load_osdeploy_pve_context_with_budget(
                grant.operation_id(),
                snapshot.revision(),
                pve_port::ProvisioningEvaluationModeV1::Reconciliation,
            )
            .await
            .unwrap();
    assert_eq!(
        observation.context().facts().binding.operation_id(),
        grant.operation_id()
    );
    assert_eq!(
        observation.context().facts().mutation_deadline,
        snapshot.deadline_at().unwrap()
    );
    assert_eq!(
        observation.remaining(),
        (snapshot.deadline_at().unwrap() - observation.checked_at())
            .to_std()
            .unwrap()
    );
    assert_eq!(s.db.snapshot().await, before);
    assert!(
        s.db.store
            .load_osdeploy_pve_context_with_budget(
                grant.operation_id(),
                snapshot.revision() - 1,
                pve_port::ProvisioningEvaluationModeV1::Reconciliation
            )
            .await
            .is_err()
    );
    assert_eq!(s.db.snapshot().await, before);
}

#[tokio::test]
async fn due_unknown_reconciles_without_new_lease_or_send() {
    let s = Scenario::new(300, true).await;
    let grant = dispatched_unknown(&s).await;
    let snapshot =
        s.db.store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
    s.wait_until(snapshot.next_check_at().unwrap()).await;
    let due = s.db.scheduler().discover_osdeploy_due().await.unwrap();
    assert_eq!(due.len(), 1);
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    assert_eq!(
        c.run_due_once(&due[0]).await.unwrap(),
        Progress::Decided(ExecutionState::Satisfied)
    );
    let after = s.db.snapshot().await;
    assert!(after["worker_leases"].as_array().unwrap().is_empty());
    assert_eq!(after["osdeploy_lease_epochs"].as_array().unwrap().len(), 1);
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}

async fn chain(growth: bool) {
    let s = Scenario::new(300, growth).await;
    for stage in [Stage::Clone, Stage::DiskCapacity, Stage::ConfigurePe] {
        let c = controller(&s);
        c.open_send_admission().await.unwrap();
        assert_eq!(
            c.run_osdeploy_once(s.ids.operation(stage)).await.unwrap(),
            Progress::Decided(ExecutionState::Satisfied)
        );
        c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    }
    assert_eq!(
        s.fake.recorded_provisioning_submissions().len(),
        if growth { 3 } else { 2 }
    );
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let before = s.db.snapshot().await;
    for stage in Stage::ALL {
        if matches!(
            stage,
            Stage::Clone | Stage::DiskCapacity | Stage::ConfigurePe
        ) {
            continue;
        }
        assert_eq!(
            c.run_osdeploy_once(s.ids.operation(stage)).await,
            Err(Error::CapabilityUnavailable)
        );
        assert_eq!(s.db.snapshot().await, before);
    }
}

#[tokio::test]
async fn reconstructed_controller_executes_growth_chain() {
    chain(true).await;
}

#[tokio::test]
async fn reconstructed_controller_executes_no_growth_chain() {
    chain(false).await;
}

#[tokio::test]
async fn response_loss_never_resends() {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, Some(op)),
            FakeMutationOutcome::AppliedResponseLost,
        )
        .unwrap();
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    c.run_osdeploy_once(op).await.unwrap();
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    c.run_osdeploy_once(op).await.unwrap();
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
    assert!(
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .receipt()
            .is_none()
    );
}

#[tokio::test]
async fn waiting_resumes_original_task_in_new_controller() {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, Some(op)),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    assert_eq!(c.run_osdeploy_once(op).await.unwrap(), Progress::Waiting);
    let parked = s.db.store.load_osdeploy_operation(op).await.unwrap();
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    assert_eq!(c.run_osdeploy_once(op).await.unwrap(), Progress::Idle);
    let pve_port::MutationReceipt::Task(upid) = parked.receipt().unwrap().receipt() else {
        panic!("actual original Clone task receipt required");
    };
    s.fake.complete_provisioning_task(upid).unwrap();
    s.wait_until(parked.next_check_at().unwrap()).await;
    let due = s.db.scheduler().discover_osdeploy_due().await.unwrap();
    assert_eq!(due.len(), 1);
    assert_eq!(
        c.run_due_once(&due[0]).await.unwrap(),
        Progress::Decided(ExecutionState::Satisfied)
    );
    let done = s.db.store.load_osdeploy_operation(op).await.unwrap();
    assert_eq!(done.attempt_id(), parked.attempt_id());
    assert_eq!(done.deadline_at(), parked.deadline_at());
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}

async fn pause_entered(
    pause: &pve_port::FakePause,
    worker: &mut tokio::task::JoinHandle<Result<Progress, Error>>,
) {
    tokio::time::timeout(Duration::from_secs(10), async {
        tokio::select! {
            () = pause.entered() => {},
            result = worker => panic!("worker completed before checkpoint: {result:?}"),
        }
    })
    .await
    .expect("checkpoint deadline");
}

async fn wait_authority_waiter(pool: &sqlx::PgPool) {
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let count: i64 = sqlx::query_scalar("SELECT count(*) FROM pg_stat_activity WHERE datid=(SELECT oid FROM pg_database WHERE datname=current_database()) AND pid<>pg_backend_pid() AND wait_event_type='Lock' AND query LIKE '%orchestration_authority%'")
                .fetch_one(pool).await.unwrap();
            if count > 0 { break; }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }).await.expect("authority waiter not observed");
}

async fn old_opens_close_race(queue_second: bool) {
    let s = Scenario::new(300, true).await;
    let c = controller(&s);
    let before = s.db.snapshot().await;
    let mut lock = s.db.pool.begin().await.unwrap();
    sqlx::query("SELECT singleton_key FROM rust_controller.orchestration_authority WHERE singleton_key=1 FOR UPDATE").execute(&mut *lock).await.unwrap();
    let old = tokio::spawn({
        let c = c.clone();
        async move { c.open_send_admission().await }
    });
    wait_authority_waiter(&s.db.pool).await;
    let mut queued = Box::pin(c.open_send_admission());
    if queue_second {
        std::future::poll_fn(|cx| {
            assert!(std::future::Future::poll(queued.as_mut(), cx).is_pending());
            std::task::Poll::Ready(())
        })
        .await;
    }
    assert_eq!(
        c.close_and_drain(Duration::from_millis(100)).await,
        Err(Error::TimedOut)
    );
    assert_eq!(
        c.run_osdeploy_once(s.ids.operation(Stage::Clone)).await,
        Err(Error::AdmissionClosed)
    );
    lock.rollback().await.unwrap();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(5), old)
            .await
            .unwrap()
            .unwrap(),
        Err(Error::AdmissionClosed)
    );
    if queue_second {
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(5), queued.as_mut())
                .await
                .unwrap(),
            Err(Error::AdmissionClosed)
        );
    }
    drop(queued);
    assert_eq!(s.db.snapshot().await, before);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    assert_eq!(
        c.run_osdeploy_once(s.ids.operation(Stage::Clone)).await,
        Err(Error::AdmissionClosed)
    );
    c.open_send_admission().await.unwrap();
    assert_eq!(
        c.run_osdeploy_once(s.ids.operation(Stage::Clone))
            .await
            .unwrap(),
        Progress::Decided(ExecutionState::Satisfied)
    );
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
}

#[tokio::test]
async fn older_open_cannot_reopen_after_close_lock_timeout() {
    old_opens_close_race(false).await;
}

#[tokio::test]
async fn queued_old_open_is_invalidated_but_fresh_explicit_open_can_succeed() {
    old_opens_close_race(true).await;
}

#[tokio::test]
async fn paused_send_drain_completes_capture_and_capacity_exhaustion_is_idle() {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    let pause = s
        .fake
        .pause_provisioning_submission(ProvisioningFaultSelectorV1::new(
            ProvisioningActionV1::Clone,
            Some(op),
        ));
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let mut worker = tokio::spawn({
        let c = c.clone();
        async move { c.run_osdeploy_once(op).await }
    });
    pause_entered(&pause, &mut worker).await;
    let before = s.db.snapshot().await;
    assert_eq!(
        c.run_osdeploy_once(s.ids.operation(Stage::DiskCapacity))
            .await
            .unwrap(),
        Progress::Idle
    );
    assert_eq!(s.db.snapshot().await, before);
    let drain = c.close_and_drain(Duration::from_secs(2));
    tokio::pin!(drain);
    std::future::poll_fn(|cx| {
        assert!(std::future::Future::poll(drain.as_mut(), cx).is_pending());
        std::task::Poll::Ready(())
    })
    .await;
    pause.release();
    drain.await.unwrap();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(1), worker)
            .await
            .unwrap()
            .unwrap(),
        Err(Error::AdmissionClosed)
    );
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
    assert!(
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .receipt()
            .is_some()
    );
}

#[tokio::test]
async fn failed_drain_stays_closed_while_original_send_finishes() {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    let pause = s
        .fake
        .pause_provisioning_submission(ProvisioningFaultSelectorV1::new(
            ProvisioningActionV1::Clone,
            Some(op),
        ));
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let mut worker = tokio::spawn({
        let c = c.clone();
        async move { c.run_osdeploy_once(op).await }
    });
    pause_entered(&pause, &mut worker).await;
    let before = s.db.snapshot().await;
    assert_eq!(
        c.close_and_drain(Duration::from_millis(100)).await,
        Err(Error::TimedOut)
    );
    assert_eq!(c.open_send_admission().await, Err(Error::AdmissionClosed));
    assert_eq!(c.run_osdeploy_once(op).await, Err(Error::AdmissionClosed));
    assert_eq!(
        s.db.snapshot().await["orchestration_authority"],
        before["orchestration_authority"]
    );
    pause.release();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(3), worker)
            .await
            .unwrap()
            .unwrap(),
        Err(Error::AdmissionClosed)
    );
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    assert!(
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .receipt()
            .is_some()
    );
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}

#[tokio::test]
async fn cancellation_after_dispatch_commit_cannot_send() {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    let pause = s
        .fake
        .pause_controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted);
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let mut worker = tokio::spawn({
        let c = c.clone();
        async move { c.run_osdeploy_once(op).await }
    });
    pause_entered(&pause, &mut worker).await;
    let run =
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .run_id();
    s.db.scheduler().cancel_osdeploy_run(run).await.unwrap();
    pause.release();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(5), worker)
            .await
            .unwrap()
            .unwrap(),
        Err(Error::FenceLost)
    );
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
}

#[tokio::test]
async fn current_thread_cancellation_drops_owned_send_future() {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    let pause = s
        .fake
        .pause_provisioning_submission(ProvisioningFaultSelectorV1::new(
            ProvisioningActionV1::Clone,
            Some(op),
        ));
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let mut worker = tokio::spawn({
        let c = c.clone();
        async move { c.run_osdeploy_once(op).await }
    });
    pause_entered(&pause, &mut worker).await;
    worker.abort();
    assert!(worker.await.unwrap_err().is_cancelled());
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    pause.release();
    assert!(
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .dispatch()
            .is_some()
    );
    // The awaited DB observation gives any accidentally detached send a turn
    // after release; the successful drain must still have left no sender.
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
}

#[tokio::test]
async fn close_at_dispatch_commit_cancels_queued_unsent_work() {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    let pause = s
        .fake
        .pause_controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted);
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let mut worker = tokio::spawn({
        let c = c.clone();
        async move { c.run_osdeploy_once(op).await }
    });
    pause_entered(&pause, &mut worker).await;
    let before = s.db.snapshot().await;
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(1), worker)
            .await
            .unwrap()
            .unwrap(),
        Err(Error::AdmissionClosed)
    );
    pause.release();
    let snapshot = s.db.store.load_osdeploy_operation(op).await.unwrap();
    assert!(snapshot.dispatch().is_some());
    assert!(snapshot.receipt().is_none());
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    assert_eq!(c.run_osdeploy_once(op).await, Err(Error::AdmissionClosed));
    assert_eq!(s.db.snapshot().await, before);
}

#[tokio::test]
async fn closed_start_cannot_activate_or_send() {
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let c = operation_controller::OsDeployController::new(
        s.db.store.clone(),
        s.db.scheduler(),
        s.fake.clone(),
        1,
    )
    .unwrap();
    let before = s.db.snapshot().await;
    assert!(matches!(
        c.run_osdeploy_once(s.ids.operation(osdeploy_adapter::OsDeployStage::Clone))
            .await,
        Err(operation_controller::OsDeployControllerError::AdmissionClosed)
    ));
    assert_eq!(s.db.snapshot().await, before);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    c.open_send_admission().await.unwrap();
    assert_eq!(
        c.run_osdeploy_once(s.ids.operation(osdeploy_adapter::OsDeployStage::Clone))
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Satisfied)
    );
}

#[tokio::test]
async fn authority_change_after_dispatch_commit_prevents_send() {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    let pause = s
        .fake
        .pause_controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted);
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let mut worker = tokio::spawn({
        let c = c.clone();
        async move { c.run_osdeploy_once(op).await }
    });
    pause_entered(&pause, &mut worker).await;
    sqlx::query(
        "UPDATE rust_controller.orchestration_authority SET generation=2,executor_kind='python'",
    )
    .execute(&s.db.pool)
    .await
    .unwrap();
    let before = s.db.snapshot().await;
    pause.release();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(5), worker)
            .await
            .unwrap()
            .unwrap(),
        Err(Error::FenceLost)
    );
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    assert_eq!(s.db.snapshot().await, before);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
}

#[tokio::test]
async fn authority_change_during_entered_send_still_captures_original_response() {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    let pause = s
        .fake
        .pause_provisioning_submission(ProvisioningFaultSelectorV1::new(
            ProvisioningActionV1::Clone,
            Some(op),
        ));
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let mut worker = tokio::spawn({
        let c = c.clone();
        async move { c.run_osdeploy_once(op).await }
    });
    pause_entered(&pause, &mut worker).await;
    sqlx::query(
        "UPDATE rust_controller.orchestration_authority SET generation=2,executor_kind='python'",
    )
    .execute(&s.db.pool)
    .await
    .unwrap();
    pause.release();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(5), worker)
            .await
            .unwrap()
            .unwrap(),
        Err(Error::FenceLost)
    );
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    let snapshot = s.db.store.load_osdeploy_operation(op).await.unwrap();
    let submissions = s.fake.recorded_provisioning_submissions();
    assert_eq!(submissions.len(), 1);
    assert_eq!(
        snapshot.receipt().unwrap().receipt(),
        submissions[0].returned().as_ref().unwrap()
    );
}

#[tokio::test]
async fn unpolled_controller_future_has_no_activity_or_durable_effect() {
    let s = Scenario::new(300, true).await;
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let before = s.db.snapshot().await;
    drop(c.run_osdeploy_once(s.ids.operation(Stage::Clone)));
    c.close_and_drain(Duration::from_millis(100)).await.unwrap();
    assert_eq!(s.db.snapshot().await, before);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
}

async fn receipt_storage_retries(failures: i64) {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    sqlx::raw_sql(&format!("CREATE SEQUENCE task8_capture_attempt; CREATE FUNCTION task8_capture_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF nextval('task8_capture_attempt') <= {failures} THEN RAISE EXCEPTION 'owned_capture_fault'; END IF; RETURN NEW; END $$; CREATE TRIGGER task8_capture_fault BEFORE INSERT ON rust_controller.osdeploy_pve_receipts FOR EACH ROW EXECUTE FUNCTION task8_capture_fault();")).execute(&s.db.pool).await.unwrap();
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let started = tokio::time::Instant::now();
    let result = tokio::time::timeout(Duration::from_secs(10), c.run_osdeploy_once(op))
        .await
        .unwrap()
        .unwrap();
    assert!(started.elapsed() >= Duration::from_millis(350));
    let attempts: i64 = sqlx::query_scalar("SELECT last_value FROM task8_capture_attempt")
        .fetch_one(&s.db.pool)
        .await
        .unwrap();
    assert_eq!(attempts, 3);
    let snapshot = s.db.store.load_osdeploy_operation(op).await.unwrap();
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
    if failures == 2 {
        assert_eq!(result, Progress::Decided(ExecutionState::Satisfied));
        assert_eq!(
            snapshot.receipt().unwrap().receipt(),
            s.fake.recorded_provisioning_submissions()[0]
                .returned()
                .as_ref()
                .unwrap()
        );
    } else {
        assert_eq!(result, Progress::Decided(ExecutionState::Unknown));
        assert!(snapshot.receipt().is_none());
    }
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
}

#[tokio::test]
async fn receipt_storage_retries_twice_then_captures_only_original_response() {
    receipt_storage_retries(2).await;
}

#[tokio::test]
async fn receipt_storage_failure_stops_after_three_attempts_without_resend() {
    receipt_storage_retries(99).await;
}

#[tokio::test]
async fn unavailable_preflight_is_durable_unknown_and_never_sends() {
    let s = Scenario::new(300, true).await;
    let c = controller(&s);
    let op = s.ids.operation(Stage::Clone);
    s.fake.enqueue_provisioning_config_read(
        pve_port::NodeName::parse("node-a").unwrap(),
        pve_port::Vmid::new(900).unwrap(),
        pve_port::FakeProvisioningConfigReadV1::Error(pve_port::PveReadError::TransportUnavailable),
    );
    c.open_send_admission().await.unwrap();
    assert_eq!(
        c.run_osdeploy_once(op).await.unwrap(),
        Progress::Decided(ExecutionState::Unknown)
    );
    assert!(
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .dispatch()
            .is_none()
    );
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
}

#[tokio::test]
async fn observation_timeout_preserves_unavailable_evidence_without_send() {
    let s = Scenario::new(300, true).await;
    let c = controller(&s);
    let op = s.ids.operation(Stage::Clone);
    let pause = Arc::new(pve_port::FakePause::default());
    s.fake.enqueue_provisioning_config_read(
        pve_port::NodeName::parse("node-a").unwrap(),
        pve_port::Vmid::new(900).unwrap(),
        pve_port::FakeProvisioningConfigReadV1::Pause(pause.clone()),
    );
    c.open_send_admission().await.unwrap();
    let mut worker = tokio::spawn({
        let c = c.clone();
        async move { c.run_osdeploy_once(op).await }
    });
    pause_entered(&pause, &mut worker).await;
    let result = tokio::time::timeout(Duration::from_secs(4), worker)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(result, Progress::Decided(ExecutionState::Unknown));
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    pause.release();
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    let text: String = sqlx::query_scalar("SELECT evidence_canonical_json FROM rust_controller.osdeploy_pve_evidence WHERE operation_id=$1").bind(op.as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    let evidence: pve_port::ProvisioningEvidenceV1 = serde_json::from_str(&text).unwrap();
    assert!(matches!(
        evidence.facts().source_config.as_ref().unwrap().result,
        Err(pve_port::PveReadError::TimedOut)
    ));
    assert!(
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .dispatch()
            .is_none()
    );
}

#[tokio::test]
async fn identity_overflow_is_partial_and_never_dispatches() {
    let s = Scenario::new(300, true).await;
    let node = pve_port::NodeName::parse("node-a").unwrap();
    for vmid in 1000..1033 {
        let config = pve_port::ProvisioningVmConfigV1::from_wire(node.clone(), pve_port::Vmid::new(vmid).unwrap(), pve_port::NativeEvidenceSource::FakePve,
            serde_json::json!({"node":"node-a","vmid":vmid,"digest":"other","name":format!("other-{vmid}"),"cores":2,"memory":2048,"scsi0":format!("disk-store:vm-{vmid}-disk-0,size=80G"),"smbios1":format!("uuid=33333333-3333-4333-8333-{vmid:012}"),"net0":format!("virtio=02:00:00:01:{:02x}:{:02x},bridge=vmbr0,firewall=0",vmid / 256,vmid % 256),"bios":"seabios","cpu":"host","balloon":0,"agent":"enabled=0,type=virtio","boot":"order=scsi0","template":0}), chrono::Utc::now()).unwrap();
        s.fake
            .insert_provisioning_vm(config, pve_port::PowerState::Stopped)
            .unwrap();
    }
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let op = s.ids.operation(Stage::Clone);
    assert_eq!(
        c.run_osdeploy_once(op).await.unwrap(),
        Progress::Decided(ExecutionState::Unknown)
    );
    let text: String = sqlx::query_scalar("SELECT evidence_canonical_json FROM rust_controller.osdeploy_pve_evidence WHERE operation_id=$1").bind(op.as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    let evidence: pve_port::ProvisioningEvidenceV1 = serde_json::from_str(&text).unwrap();
    assert_eq!(
        evidence.facts().inventory_coverage,
        pve_port::ProvisioningCoverageV1::Partial
    );
    assert_eq!(evidence.facts().identities.len(), 32);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
}

async fn wait_query_lock(pool: &sqlx::PgPool, pattern: &str) {
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let waiting: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event_type='Lock' AND query LIKE $1)").bind(pattern).fetch_one(pool).await.unwrap();
            if waiting { break; }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }).await.expect("owned query never reached expected lock");
}

#[tokio::test]
async fn delayed_unknown_context_returns_zero_and_expired_due_cannot_read() {
    use pve_port::ProvisioningFakePort;
    let s = Scenario::new(8, true).await;
    let grant = dispatched_unknown(&s).await;
    let snap =
        s.db.store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
    s.wait_until(snap.next_check_at().unwrap()).await;
    let due =
        s.db.scheduler()
            .discover_osdeploy_due()
            .await
            .unwrap()
            .remove(0);
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let before = s.db.snapshot().await;
    let mut lock = s.db.pool.begin().await.unwrap();
    sqlx::query("LOCK TABLE rust_controller.osdeploy_pve_evidence IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *lock)
        .await
        .unwrap();
    let store = s.db.store.clone();
    let operation = grant.operation_id();
    let revision = snap.revision();
    let mut worker = tokio::spawn(async move {
        store
            .load_osdeploy_pve_context_with_budget(
                operation,
                revision,
                pve_port::ProvisioningEvaluationModeV1::Reconciliation,
            )
            .await
    });
    tokio::select! {
        () = wait_query_lock(&s.db.pool, "%osdeploy_pve_evidence%") => {},
        _ = &mut worker => panic!("context completed before lock"),
    }
    s.wait_until(snap.deadline_at().unwrap()).await;
    lock.rollback().await.unwrap();
    let observation = tokio::time::timeout(Duration::from_secs(3), worker)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(observation.remaining(), Duration::ZERO);
    assert_eq!(
        observation.context().facts().mutation_deadline,
        snap.deadline_at().unwrap()
    );
    let sentinel = Arc::new(pve_port::FakePause::default());
    let node = pve_port::NodeName::parse("node-a").unwrap();
    let vmid = pve_port::Vmid::new(900).unwrap();
    s.fake.enqueue_provisioning_identity_read(
        node.clone(),
        vmid,
        pve_port::FakeProvisioningIdentityReadV1::Pause(sentinel.clone()),
    );
    assert_eq!(c.run_due_once(&due).await.unwrap(), Progress::Idle);
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    assert_eq!(s.db.snapshot().await, before);
    // Prove the original read sentinel remains queued, rather than inferring
    // absence of reads from a mutation counter or an unchanged DB snapshot.
    let read = s.fake.provisioning_identity(&node, vmid);
    tokio::pin!(read);
    tokio::time::timeout(Duration::from_secs(1), async {
        tokio::select! {
            result = &mut read => panic!("sentinel was consumed earlier: {result:?}"),
            () = sentinel.entered() => {},
        }
    })
    .await
    .unwrap();
    sentinel.release();
    read.await.unwrap();
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}

#[tokio::test]
async fn unknown_budget_loader_rejects_invalid_original_history_without_writes() {
    let s = Scenario::new(300, true).await;
    let grant = dispatched_unknown(&s).await;
    let snap =
        s.db.store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
    s.db.corrupt_immutable("osdeploy_pve_evidence", &format!("UPDATE rust_controller.osdeploy_pve_evidence SET evidence_canonical_json='{{}}' WHERE operation_id='{}'", grant.operation_id().as_uuid())).await;
    let before = s.db.snapshot().await;
    assert!(matches!(
        s.db.store
            .load_osdeploy_pve_context_with_budget(
                grant.operation_id(),
                snap.revision(),
                pve_port::ProvisioningEvaluationModeV1::Reconciliation
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
    assert_eq!(s.db.snapshot().await, before);
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}

#[tokio::test]
async fn lease_status_roundtrip_delay_consumes_original_interval() {
    let s = Scenario::new(30, true).await;
    let grant = s.started(Stage::Clone).await;
    let original = *grant.deadline_at();
    let mut lock = s.db.pool.begin().await.unwrap();
    sqlx::query("SELECT singleton_key FROM rust_controller.orchestration_authority FOR UPDATE")
        .execute(&mut *lock)
        .await
        .unwrap();
    let started = tokio::time::Instant::now();
    let scheduler = s.db.scheduler();
    let hash = s.ids.workflow_sha256().to_owned();
    let worker =
        tokio::spawn(async move { scheduler.continuation_osdeploy_bound(&grant, &hash).await });
    wait_authority_waiter(&s.db.pool).await;
    // Deliberate round-trip delay follows a confirmed owned DB lock waiter.
    tokio::time::sleep_until(started + Duration::from_millis(500)).await;
    lock.rollback().await.unwrap();
    let status = tokio::time::timeout(Duration::from_secs(3), worker)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let elapsed = started.elapsed();
    assert!(elapsed >= Duration::from_millis(500));
    assert_eq!(*status.grant().deadline_at(), original);
    assert!(status.remaining() < Duration::from_secs(30));
    assert!(
        status.remaining().saturating_sub(elapsed)
            <= status
                .remaining()
                .saturating_sub(Duration::from_millis(500))
    );
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
}

#[tokio::test]
async fn late_physical_success_cannot_reconcile_expired_unknown() {
    let s = Scenario::new(5, true).await;
    let op = s.ids.operation(Stage::Clone);
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, Some(op)),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let grant = dispatched_unknown(&s).await;
    let snapshot = s.db.store.load_osdeploy_operation(op).await.unwrap();
    let pve_port::MutationReceipt::Task(upid) = snapshot.receipt().unwrap().receipt() else {
        panic!("original task required");
    };
    s.wait_until(snapshot.deadline_at().unwrap()).await;
    s.fake.complete_provisioning_task(upid).unwrap();
    let (event, revision) = s.reconciliation_observation(&grant).await;
    let before = s.db.snapshot().await;
    assert!(matches!(
        s.db.scheduler()
            .reconcile_osdeploy_unknown(
                op,
                grant.attempt_id(),
                revision,
                event,
                s.ids.workflow_sha256()
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert_eq!(s.db.snapshot().await, before);
    assert_eq!(
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .state(),
        ExecutionState::Unknown
    );
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}

#[tokio::test]
async fn original_whole_endpoint_drops_queued_unsent_permit() {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    let pause = s
        .fake
        .pause_controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted);
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let started = tokio::time::Instant::now();
    let mut worker = tokio::spawn({
        let c = c.clone();
        async move { c.run_osdeploy_once(op).await }
    });
    pause_entered(&pause, &mut worker).await;
    assert_eq!(
        tokio::time::timeout_at(started + Duration::from_secs(26), worker)
            .await
            .unwrap()
            .unwrap(),
        Err(Error::TimedOut)
    );
    assert!(started.elapsed() >= Duration::from_secs(24));
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    pause.release();
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    assert!(
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .dispatch()
            .is_some()
    );
}

#[tokio::test]
async fn delayed_original_response_capture_keeps_original_whole_endpoint() {
    let s = Scenario::new(300, true).await;
    let op = s.ids.operation(Stage::Clone);
    let committed = s
        .fake
        .pause_controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted);
    let sending = s
        .fake
        .pause_provisioning_submission(ProvisioningFaultSelectorV1::new(
            ProvisioningActionV1::Clone,
            Some(op),
        ));
    let c = controller(&s);
    c.open_send_admission().await.unwrap();
    let started = tokio::time::Instant::now();
    let mut worker = tokio::spawn({
        let c = c.clone();
        async move { c.run_osdeploy_once(op).await }
    });
    pause_entered(&committed, &mut worker).await;
    tokio::time::sleep_until(started + Duration::from_secs(1)).await;
    committed.release();
    pause_entered(&sending, &mut worker).await;
    let mut lock = s.db.pool.begin().await.unwrap();
    sqlx::query("LOCK TABLE rust_controller.osdeploy_pve_receipts IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *lock)
        .await
        .unwrap();
    sending.release();
    wait_query_lock(&s.db.pool, "%osdeploy_pve_receipts%").await;
    let result = tokio::time::timeout_at(started + Duration::from_secs(26), worker)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(result, Err(Error::TimedOut));
    assert!(started.elapsed() >= Duration::from_secs(24));
    lock.rollback().await.unwrap();
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
    assert!(
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .receipt()
            .is_none()
    );
}

#[tokio::test]
async fn cancellation_during_collection_cannot_relabel_old_evidence() {
    let s = Scenario::new(300, true).await;
    let c = controller(&s);
    let op = s.ids.operation(Stage::Clone);
    let pause = Arc::new(pve_port::FakePause::default());
    s.fake.enqueue_provisioning_config_read(
        pve_port::NodeName::parse("node-a").unwrap(),
        pve_port::Vmid::new(900).unwrap(),
        pve_port::FakeProvisioningConfigReadV1::Pause(pause.clone()),
    );
    c.open_send_admission().await.unwrap();
    let mut worker = tokio::spawn({
        let c = c.clone();
        async move { c.run_osdeploy_once(op).await }
    });
    pause_entered(&pause, &mut worker).await;
    let run =
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .run_id();
    s.db.scheduler().cancel_osdeploy_run(run).await.unwrap();
    let before = s.db.snapshot().await;
    pause.release();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(5), worker)
            .await
            .unwrap()
            .unwrap(),
        Err(Error::FenceLost)
    );
    c.close_and_drain(Duration::from_secs(1)).await.unwrap();
    assert_eq!(s.db.snapshot().await, before);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
}

#[tokio::test]
async fn delayed_unknown_reconstruction_samples_db_after_the_wait() {
    let s = Scenario::new(30, true).await;
    let grant = dispatched_unknown(&s).await;
    let snapshot =
        s.db.store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
    let before = s.db.snapshot().await;
    let mut lock = s.db.pool.begin().await.unwrap();
    sqlx::query("LOCK TABLE rust_controller.osdeploy_pve_evidence IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *lock)
        .await
        .unwrap();
    let store = s.db.store.clone();
    let op = grant.operation_id();
    let revision = snapshot.revision();
    let started = tokio::time::Instant::now();
    let worker = tokio::spawn(async move {
        store
            .load_osdeploy_pve_context_with_budget(
                op,
                revision,
                pve_port::ProvisioningEvaluationModeV1::Reconciliation,
            )
            .await
    });
    wait_query_lock(&s.db.pool, "%osdeploy_pve_evidence%").await;
    tokio::time::sleep_until(started + Duration::from_millis(500)).await;
    let released_at: chrono::DateTime<chrono::Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut *lock)
        .await
        .unwrap();
    lock.rollback().await.unwrap();
    let observation = tokio::time::timeout(Duration::from_secs(3), worker)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let elapsed = started.elapsed();
    assert!(elapsed >= Duration::from_millis(500));
    assert!(observation.checked_at() >= released_at);
    assert_eq!(
        observation.context().facts().mutation_deadline,
        snapshot.deadline_at().unwrap()
    );
    assert_eq!(
        observation.remaining(),
        (snapshot.deadline_at().unwrap() - observation.checked_at())
            .to_std()
            .unwrap()
    );
    assert!(observation.remaining().saturating_sub(elapsed) < observation.remaining());
    assert_eq!(s.db.snapshot().await, before);
}
