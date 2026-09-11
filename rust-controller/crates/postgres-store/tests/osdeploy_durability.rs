mod osdeploy_execution_support;
#[allow(
    dead_code,
    reason = "shared registration support has other harness consumers"
)]
mod osdeploy_support;

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn fixture_credential_aliases_replay_renew_rollback_and_reopen() {
    use osdeploy_adapter::OsDeployStage;
    use postgres_store::{ExecutorKind, OsDeployExecutionError, Scheduler};
    use pve_port::ProvisioningEvaluationModeV1;
    let s = osdeploy_execution_support::Scenario::fixture_owned().await;
    for stage in [
        OsDeployStage::Clone,
        OsDeployStage::DiskCapacity,
        OsDeployStage::ConfigurePe,
    ] {
        assert_eq!(
            s.finish_stage(stage).await,
            controller_domain::ExecutionState::Satisfied
        );
    }
    let scheduler = s.db.scheduler().with_fixture_start_pe();
    let op = s.ids.operation(OsDeployStage::StartPe);
    let grant = scheduler
        .claim_osdeploy_bound(op, s.ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    scheduler
        .start_osdeploy_bound(&grant, s.ids.workflow_sha256())
        .await
        .unwrap();
    let expiry = chrono::Utc::now().timestamp() + 600;
    let secret = b"fixture-only-test-signing-secret";
    // Registration alone has no session and cannot yield a credential.
    assert!(
        scheduler
            .issue_fixture_pe_credential(&grant, expiry, secret)
            .await
            .is_err()
    );
    let snapshot = s.db.store.load_osdeploy_operation(op).await.unwrap();
    let context =
        s.db.store
            .load_osdeploy_pve_context(
                op,
                snapshot.revision(),
                ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let evidence = s.collect(&context).await;
    let event =
        s.db.store
            .record_osdeploy_pve_evidence(op, grant.attempt_id(), snapshot.revision(), &evidence)
            .await
            .unwrap();
    let revision =
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .revision();
    let request =
        s.db.store
            .prepare_osdeploy_pve_request(op, revision, event)
            .await
            .unwrap();
    let _dispatch = scheduler
        .begin_osdeploy_pve_dispatch(&grant, revision, event, &request)
        .await
        .unwrap();
    let original: (uuid::Uuid, String, chrono::DateTime<chrono::Utc>) = sqlx::query_as("SELECT attempt_id,package_sha256,registration_deadline FROM rust_controller.fixture_pe_boot_sessions WHERE operation_id=$1").bind(op.as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    // An AFTER INSERT failure proves the alias write and returned credential
    // cannot escape a rolled back transaction.
    sqlx::raw_sql("CREATE FUNCTION rust_controller.reject_fixture_alias() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected alias failure'; END $$; CREATE TRIGGER reject_fixture_alias AFTER INSERT ON rust_controller.fixture_pe_credential_aliases FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_fixture_alias();").execute(&s.db.pool).await.unwrap();
    assert!(
        scheduler
            .issue_fixture_pe_credential(&grant, expiry, secret)
            .await
            .is_err()
    );
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.fixture_pe_credential_aliases")
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
    assert_eq!(count, 0);
    sqlx::raw_sql("DROP TRIGGER reject_fixture_alias ON rust_controller.fixture_pe_credential_aliases; DROP FUNCTION rust_controller.reject_fixture_alias();").execute(&s.db.pool).await.unwrap();
    let (a, b) = tokio::join!(
        scheduler.issue_fixture_pe_credential(&grant, expiry, secret),
        scheduler.issue_fixture_pe_credential(&grant, expiry, secret)
    );
    let a = a.unwrap();
    let b = b.unwrap();
    assert_eq!(a.expose_for_delivery(), b.expose_for_delivery());
    assert!(!format!("{a:?}").contains(a.expose_for_delivery()));
    let reopened = Scheduler::new(s.db.other.clone(), ExecutorKind::Rust, 1, "osdeploy-worker")
        .unwrap()
        .with_fixture_start_pe();
    let replay = reopened
        .issue_fixture_pe_credential(&grant, expiry, secret)
        .await
        .unwrap();
    assert_eq!(a.expose_for_delivery(), replay.expose_for_delivery());
    let renewal = reopened
        .issue_fixture_pe_credential(&grant, expiry + 60, secret)
        .await
        .unwrap();
    assert_ne!(a.expose_for_delivery(), renewal.expose_for_delivery());
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.fixture_pe_credential_aliases")
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
    assert_eq!(count, 2);
    let retained: (uuid::Uuid, String, chrono::DateTime<chrono::Utc>) = sqlx::query_as("SELECT attempt_id,package_sha256,registration_deadline FROM rust_controller.fixture_pe_boot_sessions WHERE operation_id=$1").bind(op.as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(retained, original);
    assert!(
        s.db.scheduler()
            .issue_fixture_pe_credential(&grant, expiry, secret)
            .await
            .is_err()
    );
    let stale = Scheduler::new(s.db.other.clone(), ExecutorKind::Rust, 2, "osdeploy-worker")
        .unwrap()
        .with_fixture_start_pe();
    assert!(matches!(
        stale
            .issue_fixture_pe_credential(&grant, expiry, secret)
            .await,
        Err(OsDeployExecutionError::FenceLost)
    ));
    assert!(
        reopened
            .issue_fixture_pe_credential(&grant, 1, secret)
            .await
            .is_err()
    );
    assert!(
        reopened
            .issue_fixture_pe_credential(&grant, expiry, b"")
            .await
            .is_err()
    );
    assert!(
        sqlx::query("DELETE FROM rust_controller.fixture_pe_credential_aliases")
            .execute(&s.db.pool)
            .await
            .is_err()
    );
    assert!(
        sqlx::query(
            "UPDATE rust_controller.fixture_pe_credential_aliases SET expires_at=expires_at+1"
        )
        .execute(&s.db.pool)
        .await
        .is_err()
    );
    assert!(
        sqlx::query("TRUNCATE rust_controller.fixture_pe_credential_aliases")
            .execute(&s.db.pool)
            .await
            .is_err()
    );
    assert!(
        sqlx::query("DELETE FROM rust_controller.fixture_pe_boot_sessions WHERE operation_id=$1")
            .bind(op.as_uuid())
            .execute(&s.db.pool)
            .await
            .is_err()
    );
    // Adversarial historical-owner fixture: occupy a future credential digest
    // with another existing operation/attempt. This is SQL test setup only;
    // the scheduler has no API that can create this substituted boot session.
    let other = s.ids.operation(OsDeployStage::ConfigurePe);
    sqlx::query("INSERT INTO rust_controller.fixture_pe_boot_sessions(operation_id,run_id,attempt_id,dispatch_event_id,package_sha256,package_canonical_bytes,original_generation,worker_id,lease_acquisition_event_id,opened_at,registration_deadline) SELECT d.operation_id,d.run_id,d.attempt_id,d.dispatch_event_id,s.package_sha256,s.package_canonical_bytes,d.original_generation,s.worker_id,d.lease_acquisition_event_id,s.opened_at,s.registration_deadline FROM rust_controller.osdeploy_pve_dispatches d CROSS JOIN rust_controller.fixture_pe_boot_sessions s WHERE d.operation_id=$1 AND s.operation_id=$2")
        .bind(other.as_uuid()).bind(op.as_uuid()).execute(&s.db.pool).await.unwrap();
    let collision = api_compat::run_bearer::issue_run_bearer(
        api_compat::run_bearer::RunBearerIdentity::Text(&s.ids.run_id().as_uuid().to_string()),
        expiry + 120,
        secret,
    )
    .unwrap();
    sqlx::query("INSERT INTO rust_controller.fixture_pe_credential_aliases(alias_sha256,operation_id,run_id,attempt_id,package_sha256,expires_at,created_at) SELECT $1,operation_id,run_id,attempt_id,package_sha256,$2,clock_timestamp() FROM rust_controller.fixture_pe_boot_sessions WHERE operation_id=$3")
        .bind(collision.metadata().alias_sha256().as_slice()).bind(expiry + 120).bind(other.as_uuid()).execute(&s.db.pool).await.unwrap();
    assert!(matches!(
        reopened
            .issue_fixture_pe_credential(&grant, expiry + 120, secret)
            .await,
        Err(OsDeployExecutionError::Conflict)
    ));
    let owner: uuid::Uuid = sqlx::query_scalar("SELECT operation_id FROM rust_controller.fixture_pe_credential_aliases WHERE alias_sha256=$1").bind(collision.metadata().alias_sha256().as_slice()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(owner, other.as_uuid());
    scheduler.cancel_osdeploy_run(s.ids.run_id()).await.unwrap();
    assert!(
        reopened
            .issue_fixture_pe_credential(&grant, expiry, secret)
            .await
            .is_err()
    );
    let count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM rust_controller.fixture_pe_credential_aliases WHERE operation_id=$1",
    )
    .bind(op.as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    assert_eq!(count, 2);
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn fixture_create_origin_is_atomic_typed_immutable_and_replayable() {
    use postgres_store::OsDeployStoreError;
    let f = osdeploy_support::Fixture::new().await;
    let p = osdeploy_support::plan();
    let key = uuid::Uuid::now_v7();
    let before = f.snapshot().await;
    assert_eq!(
        f.store
            .create_fixture_osdeploy(uuid::Uuid::nil(), &p)
            .await
            .unwrap_err(),
        OsDeployStoreError::Validation
    );
    // Fail downstream of the origin insert. Every registration row must roll back.
    sqlx::raw_sql("CREATE FUNCTION rust_controller.reject_fixture_create() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected registration failure'; END $$; CREATE TRIGGER reject_fixture_create BEFORE INSERT ON rust_controller.osdeploy_operation_plans FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_fixture_create();").execute(&f.pool).await.unwrap();
    assert!(f.store.create_fixture_osdeploy(key, &p).await.is_err());
    assert_eq!(f.snapshot().await, before);
    let origins: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.fixture_osdeploy_origins")
            .fetch_one(&f.pool)
            .await
            .unwrap();
    assert_eq!(origins, 0);
    sqlx::raw_sql("DROP TRIGGER reject_fixture_create ON rust_controller.osdeploy_operation_plans; DROP FUNCTION rust_controller.reject_fixture_create();").execute(&f.pool).await.unwrap();
    // Concurrent deliveries of a create request allocate one server-owned run.
    let (a, b) = tokio::join!(
        f.store.create_fixture_osdeploy(key, &p),
        f.other.create_fixture_osdeploy(key, &p)
    );
    let created = a.unwrap();
    assert_eq!(created, b.unwrap());
    assert_ne!(created.ids().run_id().as_uuid(), key);
    assert_eq!(
        created.text_identity(),
        created.ids().run_id().as_uuid().to_string()
    );
    assert_eq!(created.source_namespace(), "rust-owned-fixture-v1");
    let row: (String,String,String,String) = sqlx::query_as("SELECT source_namespace,claim_kind,claim_value,workflow_sha256 FROM rust_controller.fixture_osdeploy_origins WHERE create_request_id=$1").bind(key).fetch_one(&f.pool).await.unwrap();
    assert_eq!(
        row,
        (
            created.source_namespace().into(),
            "text".into(),
            created.text_identity().into(),
            created.ids().workflow_sha256().into()
        )
    );
    let after = f.snapshot().await;
    let changed = osdeploy_support::altered(|v| {
        v["policy"]["registration_seconds"] = serde_json::json!(2401)
    });
    assert_eq!(
        f.other
            .create_fixture_osdeploy(key, &changed)
            .await
            .unwrap_err(),
        OsDeployStoreError::Conflict
    );
    // A different request cannot claim the same reserved VM or manufacture an alias.
    assert_eq!(
        f.other
            .create_fixture_osdeploy(uuid::Uuid::now_v7(), &p)
            .await
            .unwrap_err(),
        OsDeployStoreError::Conflict
    );
    assert_eq!(f.snapshot().await, after);
    for (source, kind, claim) in [
        ("rust-owned-fixture-v1", "integer", created.text_identity()),
        ("rust-owned-fixture-v1", "text", "042"),
        ("foreign-database", "text", created.text_identity()),
    ] {
        let error = sqlx::query("INSERT INTO rust_controller.fixture_osdeploy_origins(create_request_id,run_id,source_namespace,claim_kind,claim_value,workflow_sha256) VALUES($1,$2,$3,$4,$5,$6)")
            .bind(uuid::Uuid::now_v7()).bind(created.ids().run_id().as_uuid()).bind(source).bind(kind).bind(claim).bind(created.ids().workflow_sha256()).execute(&f.pool).await.unwrap_err();
        assert_eq!(
            error.as_database_error().unwrap().code().as_deref(),
            Some("23514")
        );
    }
    for statement in [
        "UPDATE rust_controller.fixture_osdeploy_origins SET claim_kind='integer'",
        "DELETE FROM rust_controller.fixture_osdeploy_origins",
        "TRUNCATE rust_controller.fixture_osdeploy_origins",
    ] {
        assert!(sqlx::query(statement).execute(&f.pool).await.is_err());
    }
    f.scheduler()
        .cancel_osdeploy_run(created.ids().run_id())
        .await
        .unwrap();
    // Reconstruct through a fresh store after cancellation/lost response. No
    // process-local handle or old bearer supplies the identity association.
    let reopened = postgres_store::PgStore::new(f.pool.clone());
    assert_eq!(
        reopened.create_fixture_osdeploy(key, &p).await.unwrap(),
        created
    );
    let origins: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.fixture_osdeploy_origins")
            .fetch_one(&f.pool)
            .await
            .unwrap();
    assert_eq!(origins, 1);
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn fixture_start_pe_atomic_arming_rollback_race_and_reload() {
    async fn observe(
        s: &osdeploy_execution_support::Scenario,
        scheduler: &postgres_store::Scheduler,
        grant: &postgres_store::LeaseGrant,
    ) -> controller_domain::ExecutionState {
        let (event, revision) = s.observation(grant).await;
        match scheduler
            .decide_osdeploy_pve(grant, revision, event)
            .await
            .unwrap()
        {
            postgres_store::OsDeployProgress::Decided(state) => state,
            postgres_store::OsDeployProgress::Waiting => controller_domain::ExecutionState::Waiting,
            postgres_store::OsDeployProgress::Idle => panic!("fixture unexpectedly idle"),
        }
    }
    use controller_domain::ExecutionState;
    use osdeploy_adapter::OsDeployStage;
    use postgres_store::{ExecutorKind, OsDeployExecutionError, Scheduler};
    use pve_port::ProvisioningEvaluationModeV1;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    for stage in [
        OsDeployStage::Clone,
        OsDeployStage::DiskCapacity,
        OsDeployStage::ConfigurePe,
    ] {
        assert_eq!(s.finish_stage(stage).await, ExecutionState::Satisfied);
    }
    let op = s.ids.operation(OsDeployStage::StartPe);
    assert!(matches!(
        s.db.scheduler()
            .claim_osdeploy_bound(op, s.ids.workflow_sha256(), 1)
            .await,
        Err(OsDeployExecutionError::CapabilityUnavailable)
    ));
    let scheduler = s.db.scheduler().with_fixture_start_pe();
    let grant = scheduler
        .claim_osdeploy_bound(op, s.ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    scheduler
        .start_osdeploy_bound(&grant, s.ids.workflow_sha256())
        .await
        .unwrap();
    let snapshot = s.db.store.load_osdeploy_operation(op).await.unwrap();
    let context =
        s.db.store
            .load_osdeploy_pve_context(
                op,
                snapshot.revision(),
                ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let evidence = s.collect(&context).await;
    let event =
        s.db.store
            .record_osdeploy_pve_evidence(op, grant.attempt_id(), snapshot.revision(), &evidence)
            .await
            .unwrap();
    let revision =
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .revision();
    let request =
        s.db.store
            .prepare_osdeploy_pve_request(op, revision, event)
            .await
            .unwrap();
    let stale = Scheduler::new(s.db.store.clone(), ExecutorKind::Rust, 2, "osdeploy-worker")
        .unwrap()
        .with_fixture_start_pe();
    assert!(matches!(
        stale
            .begin_osdeploy_pve_dispatch(&grant, revision, event, &request)
            .await,
        Err(OsDeployExecutionError::FenceLost)
    ));
    assert!(matches!(
        scheduler
            .begin_osdeploy_pve_dispatch(&grant, revision + 1, event, &request)
            .await,
        Err(OsDeployExecutionError::FenceLost)
    ));
    // Fail after session insertion, at the following dispatch insert.
    sqlx::raw_sql("CREATE FUNCTION rust_controller.reject_fixture_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected fixture dispatch failure'; END $$; CREATE TRIGGER reject_fixture_dispatch BEFORE INSERT ON rust_controller.osdeploy_pve_dispatches FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_fixture_dispatch();").execute(&s.db.pool).await.unwrap();
    assert!(
        scheduler
            .begin_osdeploy_pve_dispatch(&grant, revision, event, &request)
            .await
            .is_err()
    );
    let rolled_back: (i64,i64) = sqlx::query_as("SELECT (SELECT count(*) FROM rust_controller.fixture_pe_boot_sessions),(SELECT count(*) FROM rust_controller.osdeploy_deadlines WHERE scope_key='pe_registration')").fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(rolled_back, (0, 0));
    assert_eq!(
        s.db.other
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .revision(),
        revision
    );
    sqlx::raw_sql("DROP TRIGGER reject_fixture_dispatch ON rust_controller.osdeploy_pve_dispatches; DROP FUNCTION rust_controller.reject_fixture_dispatch();").execute(&s.db.pool).await.unwrap();
    let (a, b) = tokio::join!(
        scheduler.begin_osdeploy_pve_dispatch(&grant, revision, event, &request),
        scheduler.begin_osdeploy_pve_dispatch(&grant, revision, event, &request)
    );
    assert_ne!(a.is_ok(), b.is_ok());
    let (permit, capture) = a.or(b).unwrap();
    // A caller-selected run ID has no trusted fixture origin, even when its
    // boot-arming record is valid.
    assert!(matches!(
        scheduler
            .issue_fixture_pe_credential(
                &grant,
                chrono::Utc::now().timestamp() + 600,
                b"fixture-only"
            )
            .await,
        Err(OsDeployExecutionError::Validation)
    ));
    let original = s.db.other.load_osdeploy_operation(op).await.unwrap();
    let session: (uuid::Uuid,uuid::Uuid,chrono::DateTime<chrono::Utc>,String) = sqlx::query_as("SELECT attempt_id,dispatch_event_id,registration_deadline,package_sha256 FROM rust_controller.fixture_pe_boot_sessions WHERE operation_id=$1").bind(op.as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(session.0, grant.attempt_id().as_uuid());
    assert!(session.2 > original.dispatch().unwrap().dispatched_at());
    // Lost commit response/restarted caller cannot authorize a second start.
    let restarted = s.db.scheduler().with_fixture_start_pe();
    assert!(
        restarted
            .begin_osdeploy_pve_dispatch(&grant, revision, event, &request)
            .await
            .is_err()
    );
    assert_eq!(
        s.db.other
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .dispatch(),
        original.dispatch()
    );
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    restarted
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let mut state = observe(&s, &restarted, &grant).await;
    for _ in 0..8 {
        if state != ExecutionState::Waiting {
            break;
        }
        let snapshot = s.db.store.load_osdeploy_operation(op).await.unwrap();
        s.wait_until(snapshot.next_check_at().unwrap()).await;
        let resumed = restarted
            .resume_osdeploy_bound(
                op,
                grant.attempt_id(),
                snapshot.revision(),
                s.ids.workflow_sha256(),
                1,
            )
            .await
            .unwrap()
            .unwrap();
        restarted
            .start_osdeploy_bound(&resumed, s.ids.workflow_sha256())
            .await
            .unwrap();
        state = observe(&s, &restarted, &resumed).await;
    }
    assert_eq!(state, ExecutionState::Satisfied);
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 4);
    let retained: (uuid::Uuid,uuid::Uuid,chrono::DateTime<chrono::Utc>,String) = sqlx::query_as("SELECT attempt_id,dispatch_event_id,registration_deadline,package_sha256 FROM rust_controller.fixture_pe_boot_sessions WHERE operation_id=$1").bind(op.as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(retained, session);
    assert!(matches!(
        restarted
            .claim_osdeploy_bound(
                s.ids.operation(OsDeployStage::PeRegister),
                s.ids.workflow_sha256(),
                1
            )
            .await,
        Err(OsDeployExecutionError::CapabilityUnavailable)
    ));
    sqlx::query("DELETE FROM rust_controller.fixture_pe_boot_sessions WHERE operation_id=$1")
        .bind(op.as_uuid())
        .execute(&s.db.pool)
        .await
        .unwrap();
    assert!(matches!(
        s.db.other.load_osdeploy_operation(op).await,
        Err(OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn dispatch_keeps_original_preflight_fence_separate_from_current_cas() {
    use pve_port::ProvisioningEvaluationModeV1;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let context =
        s.db.store
            .load_osdeploy_pve_context(
                r.grant.operation_id(),
                r.revision,
                ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let later = s.collect(&context).await;
    s.db.store
        .record_osdeploy_pve_evidence(
            r.grant.operation_id(),
            r.grant.attempt_id(),
            r.revision,
            &later,
        )
        .await
        .unwrap();
    let snap =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert_eq!(snap.revision(), r.revision + 1);
    let (permit, capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, snap.revision(), r.event, &r.request)
            .await
            .unwrap();
    drop((permit, capture));
    let snap =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    let dispatch = snap.dispatch().unwrap();
    assert_eq!(dispatch.preflight_event_id(), r.event);
    assert_eq!(
        dispatch.request().binding().evidence_fence(),
        (r.revision - 1) as u64
    );
    assert_eq!(dispatch.dispatch_revision(), (r.revision + 2) as u64);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    assert!(
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, snap.revision(), r.event, &r.request)
            .await
            .is_err()
    );
}

#[tokio::test]
async fn dispatch_final_clock_rejects_evidence_that_aged_during_writes() {
    let s = osdeploy_execution_support::Scenario::with_freshness(300, true, 1).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    sqlx::raw_sql("CREATE FUNCTION dispatch_age_evidence() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(1.05); RETURN NEW; END $$; CREATE TRIGGER dispatch_age_evidence BEFORE INSERT ON rust_controller.osdeploy_pve_dispatches FOR EACH ROW EXECUTE FUNCTION dispatch_age_evidence();").execute(&s.db.pool).await.unwrap();
    let before = s.db.snapshot().await;
    assert!(matches!(
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
    assert_eq!(s.db.snapshot().await, before);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
}

// A run-control proof for original-response admission only. This deliberately
// does not claim Task7 atomic all-stage cancellation or alter selected states.
async fn owned_cancellation_control(
    s: &osdeploy_execution_support::Scenario,
    grant: &postgres_store::LeaseGrant,
) {
    let snapshot =
        s.db.store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
    let mut tx = s.db.pool.begin().await.unwrap();
    let at: chrono::DateTime<chrono::Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut *tx)
        .await
        .unwrap();
    let event = controller_domain::EventId::new();
    let payload = serde_json::json!({"contract_version":1,"action":"run_cancelled","run_id":s.ids.run_id(),
        "operation_id":grant.operation_id(),"workflow_sha256":s.ids.workflow_sha256(),"stage_sha256":snapshot.plan().fingerprint().unwrap(),
        "attempt_id":grant.attempt_id(),"generation":1,"before_revision":snapshot.revision(),"evaluated_at":at,"resolution":null,
        "detail":{"reason":"run_cancellation_requested"}});
    let revision = snapshot.revision() + 1;
    let journal = event_journal::JournalEvent::new(
        event,
        grant.operation_id(),
        Some(grant.attempt_id()),
        revision,
        "owned:cancellation-control".to_owned(),
        event_journal::payload_digest(&payload).unwrap(),
        event_journal::EventKind::DecisionRecorded,
        payload.clone(),
        at,
    )
    .unwrap();
    sqlx::query("INSERT INTO rust_controller.journal_events(event_id,operation_id,attempt_id,aggregate_revision,semantic_key,payload_digest,event_kind,payload,observed_at) VALUES($1,$2,$3,$4,$5,$6,'decision_recorded',$7,$8)")
        .bind(event.as_uuid()).bind(grant.operation_id().as_uuid()).bind(grant.attempt_id().as_uuid()).bind(revision).bind(journal.semantic_key()).bind(journal.payload_digest()).bind(&payload).bind(at).execute(&mut *tx).await.unwrap();
    sqlx::query("INSERT INTO rust_controller.outbox(event_id,operation_id,topic,payload) VALUES($1,$2,'journal_event',$3)")
        .bind(event.as_uuid()).bind(grant.operation_id().as_uuid()).bind(serde_json::to_value(journal).unwrap()).execute(&mut *tx).await.unwrap();
    sqlx::query("INSERT INTO rust_controller.osdeploy_decisions(operation_id,decision_revision,event_id,run_id,attempt_id,action,resolution,workflow_sha256,stage_sha256,generation,evaluated_at,payload_canonical_json) VALUES($1,$2,$3,$4,$5,'run_cancelled',NULL,$6,$7,1,$8,$9)")
        .bind(grant.operation_id().as_uuid()).bind(revision).bind(event.as_uuid()).bind(s.ids.run_id().as_uuid()).bind(grant.attempt_id().as_uuid())
        .bind(s.ids.workflow_sha256()).bind(snapshot.plan().fingerprint().unwrap()).bind(at).bind(String::from_utf8(event_journal::canonical_json_bytes(&payload).unwrap()).unwrap()).execute(&mut *tx).await.unwrap();
    sqlx::query("INSERT INTO rust_controller.osdeploy_run_cancellations(run_id,anchor_operation_id,decision_event_id,generation,requested_at) VALUES($1,$2,$3,1,$4)")
        .bind(s.ids.run_id().as_uuid()).bind(grant.operation_id().as_uuid()).bind(event.as_uuid()).bind(at).execute(&mut *tx).await.unwrap();
    sqlx::query("UPDATE rust_controller.operations SET revision=$2 WHERE operation_id=$1")
        .bind(grant.operation_id().as_uuid())
        .bind(revision)
        .execute(&mut *tx)
        .await
        .unwrap();
    sqlx::query("UPDATE rust_controller.operation_projection SET revision=$2,last_event_id=$3 WHERE operation_id=$1").bind(grant.operation_id().as_uuid()).bind(revision).bind(event.as_uuid()).execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    assert!(
        s.db.store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap()
            .cancelled()
    );
}

#[tokio::test]
async fn original_receipt_survives_cancellation_control_while_continuation_is_fenced() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let pause = s
        .fake
        .pause_provisioning_submission(ProvisioningFaultSelectorV1::new(
            ProvisioningActionV1::Clone,
            Some(r.grant.operation_id()),
        ));
    let send = permit.submit_fake_once(s.fake.as_ref());
    tokio::pin!(send);
    tokio::time::timeout(std::time::Duration::from_secs(3),async {
        tokio::select! { _=pause.entered()=>{}, result=&mut send=>panic!("submission completed before pause: {result:?}") }
    }).await.unwrap();
    owned_cancellation_control(&s, &r.grant).await;
    assert!(matches!(
        scheduler
            .continuation_osdeploy_bound(&r.grant, s.ids.workflow_sha256())
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    pause.release();
    let receipt = tokio::time::timeout(std::time::Duration::from_secs(3), send)
        .await
        .unwrap()
        .unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let snap =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert!(snap.cancelled());
    assert_eq!(snap.state(), controller_domain::ExecutionState::Running);
    assert_eq!(snap.receipt().unwrap().receipt(), &receipt);
    assert!(matches!(
        scheduler
            .begin_osdeploy_pve_dispatch(&r.grant, snap.revision(), r.event, &r.request)
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}

#[tokio::test]
async fn dispatch_rejects_changed_request_owner_event_and_revision_without_writes() {
    use postgres_store::OsDeployExecutionError as E;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let before = s.db.snapshot().await;
    let scheduler = s.db.scheduler();
    assert!(matches!(
        scheduler
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision - 1, r.event, &r.request)
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        s.db.other_scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        scheduler
            .begin_osdeploy_pve_dispatch(
                &r.grant,
                r.revision,
                controller_domain::EventId::new(),
                &r.request
            )
            .await,
        Err(E::Validation)
    ));
    // Use the concrete variant's validated constructor via serde, preserving
    // every binding while replacing the deterministic original clone marker.
    let pve_port::ProvisioningMutationRequestV1::Clone(clone) = &r.request else {
        panic!("clone");
    };
    let mut changed_clone = serde_json::to_value(clone.clone_request()).unwrap();
    changed_clone["request_marker"] =
        serde_json::json!(controller_domain::OperationId::new().as_uuid());
    let changed = pve_port::CloneProvisioningRequestV1::new(
        r.request.binding().clone(),
        r.request.plan().clone(),
        serde_json::from_value(changed_clone).unwrap(),
        r.request.expected_before().clone(),
        chrono::Utc::now(),
        30,
    )
    .unwrap();
    assert!(matches!(
        scheduler
            .begin_osdeploy_pve_dispatch(
                &r.grant,
                r.revision,
                r.event,
                &pve_port::ProvisioningMutationRequestV1::Clone(changed)
            )
            .await,
        Err(E::Validation)
    ));
    assert_eq!(s.db.snapshot().await, before);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    sqlx::query("UPDATE rust_controller.orchestration_authority SET generation=2")
        .execute(&s.db.pool)
        .await
        .unwrap();
    let before = s.db.snapshot().await;
    assert!(matches!(
        scheduler
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await,
        Err(E::FenceLost)
    ));
    assert_eq!(s.db.snapshot().await, before);
}

#[tokio::test]
async fn dispatch_rolls_back_every_write_and_deferred_commit_failure() {
    use postgres_store::OsDeployExecutionError as E;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    sqlx::raw_sql("CREATE SEQUENCE dispatch_write_number; CREATE TABLE dispatch_fault(target bigint); INSERT INTO dispatch_fault VALUES(0); CREATE FUNCTION dispatch_fail_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF nextval('dispatch_write_number')=(SELECT target FROM dispatch_fault) THEN RAISE EXCEPTION 'owned_dispatch_fault'; END IF; RETURN NEW; END $$;").execute(&s.db.pool).await.unwrap();
    let tables = [
        "journal_events",
        "outbox",
        "osdeploy_decisions",
        "operations",
        "operation_projection",
        "osdeploy_pve_dispatches",
    ];
    for table in tables {
        sqlx::query(&format!("CREATE TRIGGER dispatch_write_fault BEFORE INSERT OR UPDATE ON rust_controller.{table} FOR EACH ROW EXECUTE FUNCTION dispatch_fail_write()"))
            .execute(&s.db.pool).await.unwrap();
    }
    let before = s.db.snapshot().await;
    // Journal, outbox, decision, revision, projection INSERT + UPDATE, dispatch.
    for boundary in 1..=7_i64 {
        sqlx::query("UPDATE dispatch_fault SET target=$1")
            .bind(boundary)
            .execute(&s.db.pool)
            .await
            .unwrap();
        sqlx::query("SELECT setval('dispatch_write_number',1,false)")
            .execute(&s.db.pool)
            .await
            .unwrap();
        assert!(
            matches!(
                s.db.scheduler()
                    .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
                    .await,
                Err(E::StorageUnavailable)
            ),
            "boundary {boundary}"
        );
        assert_eq!(s.db.snapshot().await, before, "boundary {boundary}");
    }
    for table in tables {
        sqlx::query(&format!(
            "DROP TRIGGER dispatch_write_fault ON rust_controller.{table}"
        ))
        .execute(&s.db.pool)
        .await
        .unwrap();
    }
    sqlx::raw_sql("CREATE FUNCTION dispatch_fail_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned_dispatch_commit_fault'; END $$; CREATE CONSTRAINT TRIGGER dispatch_commit_fault AFTER INSERT ON rust_controller.osdeploy_pve_dispatches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION dispatch_fail_commit();").execute(&s.db.pool).await.unwrap();
    assert!(matches!(
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await,
        Err(E::StorageUnavailable)
    ));
    assert_eq!(s.db.snapshot().await, before);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    sqlx::query("DROP TRIGGER dispatch_commit_fault ON rust_controller.osdeploy_pve_dispatches")
        .execute(&s.db.pool)
        .await
        .unwrap();
    let (permit, capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    drop((permit, capture));
}

#[tokio::test]
async fn dispatch_final_clock_rejects_scope_expiry_during_writes() {
    let s = osdeploy_execution_support::Scenario::new(1, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    sqlx::raw_sql("CREATE FUNCTION dispatch_delay() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(1.05); RETURN NEW; END $$; CREATE TRIGGER dispatch_delay BEFORE INSERT ON rust_controller.osdeploy_pve_dispatches FOR EACH ROW EXECUTE FUNCTION dispatch_delay();").execute(&s.db.pool).await.unwrap();
    let before = s.db.snapshot().await;
    assert!(matches!(
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert_eq!(s.db.snapshot().await, before);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
}

#[tokio::test]
async fn dispatch_independent_pools_commit_only_one_permit() {
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let first = s.db.scheduler();
    let second = postgres_store::Scheduler::new(
        s.db.other.clone(),
        postgres_store::ExecutorKind::Rust,
        1,
        "osdeploy-worker",
    )
    .unwrap();
    let (a, b) = tokio::time::timeout(std::time::Duration::from_secs(10), async {
        tokio::join!(
            first.begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request),
            second.begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        )
    })
    .await
    .unwrap();
    assert_ne!(a.is_ok(), b.is_ok());
    let (permit, capture) = a.or(b).ok().unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    first
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.osdeploy_pve_dispatches")
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
    assert_eq!(count, 1);
}

#[tokio::test]
async fn receipt_rejects_mismatched_task_and_conflicting_admitted_response() {
    use postgres_store::OsDeployExecutionError as E;
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    let before = s.db.snapshot().await;
    for wrong in [
        MutationReceipt::SynchronousAccepted,
        MutationReceipt::Task(
            Upid::parse("UPID:node-b:00000001:00000001:00000001:qmclone:900:root@pam:").unwrap(),
        ),
        MutationReceipt::Task(
            Upid::parse("UPID:node-a:00000001:00000001:00000001:resize:900:root@pam:").unwrap(),
        ),
        MutationReceipt::Task(
            Upid::parse("UPID:node-a:00000001:00000001:00000001:qmclone:901:root@pam:").unwrap(),
        ),
    ] {
        assert!(matches!(
            scheduler
                .record_osdeploy_pve_receipt(&capture, &wrong)
                .await,
            Err(E::Validation)
        ));
        assert_eq!(s.db.snapshot().await, before);
    }
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let before = s.db.snapshot().await;
    let different = MutationReceipt::Task(
        Upid::parse("UPID:node-a:000000FF:000000FF:000000FF:qmclone:900:root@pam:").unwrap(),
    );
    assert_ne!(receipt, different);
    assert!(matches!(
        scheduler
            .record_osdeploy_pve_receipt(&capture, &different)
            .await,
        Err(E::Conflict)
    ));
    assert_eq!(s.db.snapshot().await, before);
}

#[tokio::test]
async fn receipt_can_arrive_after_original_generation_lease_and_scope_expire() {
    let s = osdeploy_execution_support::Scenario::new(1, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    sqlx::query(
        "UPDATE rust_controller.orchestration_authority SET generation=2,executor_kind='python'",
    )
    .execute(&s.db.pool)
    .await
    .unwrap();
    sqlx::query("SELECT pg_sleep(1.05)")
        .execute(&s.db.pool)
        .await
        .unwrap();
    assert!(matches!(
        scheduler
            .continuation_osdeploy_bound(&r.grant, s.ids.workflow_sha256())
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let snapshot =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert!(snapshot.receipt().unwrap().accepted_at() >= *r.grant.deadline_at());
    assert_eq!(snapshot.state(), controller_domain::ExecutionState::Running);
    let before = s.db.snapshot().await;
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    assert_eq!(s.db.snapshot().await, before);
}

#[tokio::test]
async fn receipt_storage_retry_rolls_back_every_write_and_preserves_first_db_time() {
    use postgres_store::OsDeployExecutionError as E;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    sqlx::raw_sql("CREATE FUNCTION receipt_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned_receipt_fault'; END $$;").execute(&s.db.pool).await.unwrap();
    let before = s.db.snapshot().await;
    for table in [
        "journal_events",
        "outbox",
        "osdeploy_pve_receipts",
        "operations",
        "operation_projection",
    ] {
        sqlx::query(&format!("CREATE TRIGGER receipt_fault BEFORE INSERT OR UPDATE ON rust_controller.{table} FOR EACH ROW EXECUTE FUNCTION receipt_fail()")).execute(&s.db.pool).await.unwrap();
        assert!(matches!(
            scheduler
                .record_osdeploy_pve_receipt(&capture, &receipt)
                .await,
            Err(E::StorageUnavailable)
        ));
        assert_eq!(s.db.snapshot().await, before);
        sqlx::query(&format!(
            "DROP TRIGGER receipt_fault ON rust_controller.{table}"
        ))
        .execute(&s.db.pool)
        .await
        .unwrap();
    }
    let earliest: chrono::DateTime<chrono::Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&s.db.pool)
        .await
        .unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let saved =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert!(saved.receipt().unwrap().accepted_at() >= earliest);
    let before = s.db.snapshot().await;
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    assert_eq!(s.db.snapshot().await, before);
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}

#[tokio::test]
async fn consuming_send_errors_and_response_loss_never_restore_a_permit() {
    use pve_port::*;
    for error in [
        PveWriteError::OutcomeUnknown,
        PveWriteError::Unauthorized,
        PveWriteError::Rejected,
        PveWriteError::Conflict,
    ] {
        let s = osdeploy_execution_support::Scenario::new(300, true).await;
        let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
        s.fake
            .enqueue_provisioning_outcome(
                ProvisioningFaultSelectorV1::new(
                    ProvisioningActionV1::Clone,
                    Some(r.grant.operation_id()),
                ),
                FakeMutationOutcome::Rejected(error),
            )
            .unwrap();
        let scheduler = s.db.scheduler();
        let (permit, capture) = scheduler
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
        let before = s.db.snapshot().await;
        assert_eq!(permit.submit_fake_once(s.fake.as_ref()).await, Err(error));
        assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
        assert_eq!(s.db.snapshot().await, before);
        let snap =
            s.db.store
                .load_osdeploy_operation(r.grant.operation_id())
                .await
                .unwrap();
        assert!(snap.dispatch().is_some() && snap.receipt().is_none());
        assert!(
            scheduler
                .begin_osdeploy_pve_dispatch(&r.grant, snap.revision(), r.event, &r.request)
                .await
                .is_err()
        );
        drop(capture);
    }
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(
                ProvisioningActionV1::Clone,
                Some(r.grant.operation_id()),
            ),
            FakeMutationOutcome::AppliedResponseLost,
        )
        .unwrap();
    let (permit, capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    assert_eq!(
        permit.submit_fake_once(s.fake.as_ref()).await,
        Err(PveWriteError::OutcomeUnknown)
    );
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
    assert!(
        s.fake.recorded_provisioning_submissions()[0]
            .acceptance()
            .is_some()
    );
    let snap =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert!(snap.receipt().is_none());
    assert!(
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, snap.revision(), r.event, &r.request)
            .await
            .is_err()
    );
    drop(capture);
}

#[tokio::test]
async fn paused_send_future_cancellation_keeps_dispatch_and_cannot_resend() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let pause = s
        .fake
        .pause_provisioning_submission(ProvisioningFaultSelectorV1::new(
            ProvisioningActionV1::Clone,
            Some(r.grant.operation_id()),
        ));
    let (permit, capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    {
        let send = permit.submit_fake_once(s.fake.as_ref());
        tokio::pin!(send);
        tokio::time::timeout(std::time::Duration::from_secs(3),async {
            tokio::select! { _=pause.entered()=>{}, result=&mut send=>panic!("submission completed before pause: {result:?}") }
        }).await.unwrap();
        // Drop the owned future while the fake awaits the barrier.
    }
    pause.release();
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    let snap =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert!(snap.dispatch().is_some() && snap.receipt().is_none());
    assert!(
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, snap.revision(), r.event, &r.request)
            .await
            .is_err()
    );
    drop(capture);
}

#[tokio::test]
async fn original_clone_receipt_is_immutable_and_preserves_preflight_replay() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let saved: String = sqlx::query_scalar("SELECT evidence_canonical_json FROM rust_controller.osdeploy_pve_evidence WHERE event_id=$1")
        .bind(r.event.as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    let evidence: ProvisioningEvidenceV1 = serde_json::from_str(&saved).unwrap();
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    assert!(matches!(receipt, MutationReceipt::Task(_)));
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let snapshot =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert_eq!(snapshot.state(), controller_domain::ExecutionState::Running);
    assert_eq!(snapshot.revision(), r.revision + 2);
    assert_eq!(snapshot.receipt().unwrap().receipt(), &receipt);
    assert!(
        snapshot.receipt().unwrap().accepted_at() >= snapshot.dispatch().unwrap().dispatched_at()
    );
    let before = s.db.snapshot().await;
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    assert_eq!(s.db.snapshot().await, before);
    assert_eq!(
        s.db.store
            .record_osdeploy_pve_evidence(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                evidence.facts().binding.evidence_fence() as i64,
                &evidence
            )
            .await
            .unwrap(),
        r.event
    );
    assert_eq!(s.db.snapshot().await, before);
    let mut changed = evidence.facts().clone();
    changed.collected_at += chrono::Duration::microseconds(1);
    assert!(matches!(
        s.db.store
            .record_osdeploy_pve_evidence(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                evidence.facts().binding.evidence_fence() as i64,
                &ProvisioningEvidenceV1::new(changed).unwrap()
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::Conflict)
    ));
    assert_eq!(s.db.snapshot().await, before);
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.osdeploy_schedule_projection")
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
    assert_eq!(count, 0);
}

#[tokio::test]
async fn committed_but_unpolled_send_never_yields_a_second_permit() {
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    drop(permit.submit_fake_once(s.fake.as_ref()));
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    let snap =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert!(snap.dispatch().is_some());
    assert!(
        scheduler
            .begin_osdeploy_pve_dispatch(&r.grant, snap.revision(), r.event, &r.request)
            .await
            .is_err()
    );
    drop(capture);
}

#[tokio::test]
async fn physical_clone_advice_uses_indexed_before_state_without_dispatch_authority() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let g = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    let snap =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    let context =
        s.db.store
            .load_osdeploy_pve_context(
                g.operation_id(),
                snap.revision(),
                ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let evidence = s.collect(&context).await;
    assert_eq!(
        evaluate_provisioning_preflight(&context, &evidence, chrono::Utc::now()).decision,
        NativeDecision::Ready
    );
    let event = s
        .db
        .store
        .record_osdeploy_pve_evidence(g.operation_id(), g.attempt_id(), snap.revision(), &evidence)
        .await
        .unwrap();
    let current =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    let before = s.db.snapshot().await;
    let request =
        s.db.store
            .prepare_osdeploy_pve_request(g.operation_id(), current.revision(), event)
            .await
            .unwrap();
    assert_eq!(
        request.expected_before().config(),
        evidence
            .facts()
            .source_config
            .as_ref()
            .unwrap()
            .result
            .as_ref()
            .unwrap()
    );
    assert_eq!(
        request.expected_before().power(),
        evidence
            .facts()
            .source_power
            .as_ref()
            .unwrap()
            .result
            .as_ref()
            .unwrap()
    );
    assert_eq!(request.binding(), &evidence.facts().binding);
    let ProvisioningMutationRequestV1::Clone(clone) = request else {
        panic!("expected clone request");
    };
    let clone_wire = serde_json::to_value(clone.clone_request()).unwrap();
    assert_eq!(
        clone_wire["request_marker"],
        json!(g.operation_id().as_uuid())
    );
    assert!(current.dispatch().is_none());
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    assert_eq!(s.db.snapshot().await, before);
    assert!(matches!(
        s.db.store
            .prepare_osdeploy_pve_request(g.operation_id(), snap.revision(), event)
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
}

#[tokio::test]
async fn typed_capture_rejects_foreign_binding_source_and_original_fence_without_writes() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let g = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    let snap =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    let context =
        s.db.store
            .load_osdeploy_pve_context(
                g.operation_id(),
                snap.revision(),
                ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let evidence = s.collect(&context).await;
    let before = s.db.snapshot().await;
    for case in 0..6 {
        let mut f = evidence.facts().clone();
        let plan = if case == 4 {
            ProvisioningOperationPlanV1::new(
                ProvisioningActionV1::EnsureCapacity,
                f.plan.expected().clone(),
            )
        } else {
            f.plan.clone()
        };
        f.binding = ProvisioningBindingV1::new(
            if case == 0 {
                controller_domain::RunId::new()
            } else {
                s.ids.run_id()
            },
            if case == 1 {
                s.ids
                    .operation(osdeploy_adapter::OsDeployStage::DiskCapacity)
            } else {
                g.operation_id()
            },
            if case == 2 {
                controller_domain::AttemptId::new()
            } else {
                g.attempt_id()
            },
            if case == 3 {
                osdeploy_support::SHA
            } else {
                s.ids.workflow_sha256()
            },
            &plan,
            (snap.revision() + i64::from(case == 5)) as u64,
        )
        .unwrap();
        f.plan = plan;
        let changed = ProvisioningEvidenceV1::new(f).unwrap();
        assert!(
            s.db.store
                .record_osdeploy_pve_evidence(
                    g.operation_id(),
                    g.attempt_id(),
                    snap.revision(),
                    &changed
                )
                .await
                .is_err(),
            "binding case {case}"
        );
        assert_eq!(s.db.snapshot().await, before);
    }
    let mut f = evidence.facts().clone();
    f.source = NativeEvidenceSource::PveApi;
    f.node = None;
    f.storage = None;
    f.bridges = None;
    f.inventory = None;
    f.inventory_coverage = ProvisioningCoverageV1::Partial;
    f.identities.clear();
    f.source_config = None;
    f.source_power = None;
    f.target_config = None;
    f.target_power = None;
    f.media.clear();
    let changed = ProvisioningEvidenceV1::new(f).unwrap();
    assert!(matches!(
        s.db.store
            .record_osdeploy_pve_evidence(
                g.operation_id(),
                g.attempt_id(),
                snap.revision(),
                &changed
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
    assert_eq!(s.db.snapshot().await, before);
    assert!(
        s.db.store
            .record_osdeploy_pve_evidence(
                g.operation_id(),
                controller_domain::AttemptId::new(),
                snap.revision(),
                &evidence
            )
            .await
            .is_err()
    );
    assert!(matches!(
        s.db.store
            .load_osdeploy_pve_context(
                s.ids.operation(osdeploy_adapter::OsDeployStage::StartPe),
                0,
                ProvisioningEvaluationModeV1::Preflight
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::CapabilityUnavailable)
    ));
    assert!(
        s.db.store
            .load_osdeploy_pve_context(
                s.ids
                    .operation(osdeploy_adapter::OsDeployStage::DiskCapacity),
                0,
                ProvisioningEvaluationModeV1::Preflight
            )
            .await
            .is_err()
    );
    assert_eq!(s.db.snapshot().await, before);
}

#[tokio::test]
async fn typed_evidence_semantic_replay_conflicts_and_generic_spoof_is_never_advice() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let g = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    let snap =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    let context =
        s.db.store
            .load_osdeploy_pve_context(
                g.operation_id(),
                snap.revision(),
                ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let evidence = s.collect(&context).await;
    let event = s
        .db
        .store
        .record_osdeploy_pve_evidence(g.operation_id(), g.attempt_id(), snap.revision(), &evidence)
        .await
        .unwrap();
    let before = s.db.snapshot().await;
    let mut changed = evidence.facts().clone();
    changed.collected_at += chrono::Duration::microseconds(1);
    let changed = ProvisioningEvidenceV1::new(changed).unwrap();
    assert!(matches!(
        s.db.store
            .record_osdeploy_pve_evidence(
                g.operation_id(),
                g.attempt_id(),
                snap.revision(),
                &changed
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::Conflict)
    ));
    assert_eq!(s.db.snapshot().await, before);
    let current =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    assert!(
        s.db.store
            .prepare_osdeploy_pve_request(
                g.operation_id(),
                current.revision(),
                controller_domain::EventId::new()
            )
            .await
            .is_err()
    );
    let next =
        s.db.store
            .load_osdeploy_pve_context(
                g.operation_id(),
                current.revision(),
                ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let other = s.collect(&next).await;
    let other_event = s
        .db
        .store
        .record_osdeploy_pve_evidence(g.operation_id(), g.attempt_id(), current.revision(), &other)
        .await
        .unwrap();
    assert_ne!(event, other_event);
    let before = s.db.snapshot().await;
    assert_eq!(
        s.db.store
            .record_osdeploy_pve_evidence(
                g.operation_id(),
                g.attempt_id(),
                snap.revision(),
                &evidence
            )
            .await
            .unwrap(),
        event
    );
    assert!(matches!(
        s.db.store
            .prepare_osdeploy_pve_request(g.operation_id(), current.revision(), event)
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert_eq!(s.db.snapshot().await, before);
    // A same-shape observation without the typed provenance index remains generic.
    let mut tx = s.db.pool.begin().await.unwrap();
    sqlx::raw_sql(
        "ALTER TABLE rust_controller.osdeploy_pve_evidence DISABLE TRIGGER osdeploy_no_mutation",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query("DELETE FROM rust_controller.osdeploy_pve_evidence WHERE event_id=$1")
        .bind(other_event.as_uuid())
        .execute(&mut *tx)
        .await
        .unwrap();
    sqlx::raw_sql(
        "ALTER TABLE rust_controller.osdeploy_pve_evidence ENABLE TRIGGER osdeploy_no_mutation",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    tx.commit().await.unwrap();
    let current =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    assert!(
        s.db.store
            .prepare_osdeploy_pve_request(g.operation_id(), current.revision(), other_event)
            .await
            .is_err()
    );
}

#[tokio::test]
async fn physical_advice_rejects_template_mismatch_and_reload_hash_corruption() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let g = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    let snap =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    let context =
        s.db.store
            .load_osdeploy_pve_context(
                g.operation_id(),
                snap.revision(),
                ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let mut facts = s.collect(&context).await.facts().clone();
    let source = facts
        .source_config
        .as_ref()
        .unwrap()
        .result
        .as_ref()
        .unwrap();
    let mut changed = serde_json::to_value(source).unwrap();
    // Name is part of the immutable source fingerprint.
    changed["name"] = json!("different-template");
    let changed: ProvisioningVmConfigV1 = serde_json::from_value(changed).unwrap();
    facts.source_config = Some(NativeRead::new(chrono::Utc::now(), Ok(changed)));
    let evidence = ProvisioningEvidenceV1::new(facts).unwrap();
    let event = s
        .db
        .store
        .record_osdeploy_pve_evidence(g.operation_id(), g.attempt_id(), snap.revision(), &evidence)
        .await
        .unwrap();
    let current =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    assert!(
        s.db.store
            .prepare_osdeploy_pve_request(g.operation_id(), current.revision(), event)
            .await
            .is_err()
    );
    let before = s.db.snapshot().await;
    s.db.corrupt_immutable("osdeploy_pve_evidence", &format!("UPDATE rust_controller.osdeploy_pve_evidence SET evidence_sha256=repeat('a',64) WHERE event_id='{}'",event.as_uuid())).await;
    assert!(matches!(
        s.db.store.load_osdeploy_operation(g.operation_id()).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
    assert!(
        s.db.store
            .prepare_osdeploy_pve_request(g.operation_id(), current.revision(), event)
            .await
            .is_err()
    );
    assert!(
        before["osdeploy_pve_dispatches"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn typed_evidence_keeps_original_event_fence() {
    use osdeploy_adapter::OsDeployStage;
    use pve_port::ProvisioningEvaluationModeV1;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let g = s.started(OsDeployStage::Clone).await;
    let snap =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    let c =
        s.db.store
            .load_osdeploy_pve_context(
                g.operation_id(),
                snap.revision(),
                ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let e = s.collect(&c).await;
    let event =
        s.db.store
            .record_osdeploy_pve_evidence(g.operation_id(), g.attempt_id(), snap.revision(), &e)
            .await
            .unwrap();
    let saved: i64 = sqlx::query_scalar(
        "SELECT aggregate_revision FROM rust_controller.journal_events WHERE event_id=$1",
    )
    .bind(event.as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    assert_eq!(e.facts().binding.evidence_fence(), (saved - 1) as u64);
    let again =
        s.db.store
            .record_osdeploy_pve_evidence(g.operation_id(), g.attempt_id(), snap.revision(), &e)
            .await
            .unwrap();
    assert_eq!(again, event);
}

#[tokio::test]
async fn typed_capture_is_atomic_at_each_table_and_duplicate_collectors_share_one_event() {
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let g = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    let snap =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    let c =
        s.db.store
            .load_osdeploy_pve_context(
                g.operation_id(),
                snap.revision(),
                pve_port::ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let evidence = s.collect(&c).await;
    sqlx::raw_sql("CREATE FUNCTION owned_evidence_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned_evidence_fault'; END $$")
        .execute(&s.db.pool).await.unwrap();
    let before = s.db.snapshot().await;
    for table in [
        "journal_events",
        "outbox",
        "osdeploy_pve_evidence",
        "operations",
        "operation_projection",
    ] {
        sqlx::query(&format!("CREATE TRIGGER owned_evidence_fault BEFORE INSERT OR UPDATE ON rust_controller.{table} FOR EACH ROW EXECUTE FUNCTION owned_evidence_fail()"))
            .execute(&s.db.pool).await.unwrap();
        assert!(
            s.db.store
                .record_osdeploy_pve_evidence(
                    g.operation_id(),
                    g.attempt_id(),
                    snap.revision(),
                    &evidence
                )
                .await
                .is_err(),
            "{table}"
        );
        assert_eq!(s.db.snapshot().await, before, "{table}");
        sqlx::query(&format!(
            "DROP TRIGGER owned_evidence_fault ON rust_controller.{table}"
        ))
        .execute(&s.db.pool)
        .await
        .unwrap();
    }
    let (first, second) = tokio::time::timeout(Duration::from_secs(10), async {
        tokio::join!(
            s.db.store.record_osdeploy_pve_evidence(
                g.operation_id(),
                g.attempt_id(),
                snap.revision(),
                &evidence
            ),
            s.db.other.record_osdeploy_pve_evidence(
                g.operation_id(),
                g.attempt_id(),
                snap.revision(),
                &evidence
            )
        )
    })
    .await
    .expect("duplicate_evidence_collectors_timeout");
    let event = first.unwrap();
    assert_eq!(second.unwrap(), event);
    let after = s.db.snapshot().await;
    assert_eq!(after["osdeploy_pve_evidence"].as_array().unwrap().len(), 1);
    let current =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    assert_eq!(current.revision(), snap.revision() + 1);
    let outbox: Value =
        sqlx::query_scalar("SELECT payload FROM rust_controller.outbox WHERE event_id=$1")
            .bind(event.as_uuid())
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
    assert_eq!(outbox["payload"], serde_json::to_value(evidence).unwrap());
    let projection: (i64, Uuid) = sqlx::query_as("SELECT revision,last_event_id FROM rust_controller.operation_projection WHERE operation_id=$1")
        .bind(g.operation_id().as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(projection, (current.revision(), event.as_uuid()));
}

#[tokio::test]
async fn physical_reload_rejects_rehashed_request_marker_and_before_state_substitution() {
    let f = Fixture::new().await;
    let source = reload_source();
    let p = osdeploy_support::altered(|v| {
        v["template_config_sha256"] = json!(source.template_fingerprint().unwrap())
    });
    let h = ReloadHistory::with_plan(&f, p).await;
    let (event, _, original) = h.dispatched(&f, source).await;
    assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
    let decision: Value =
        sqlx::query_scalar("SELECT payload FROM rust_controller.journal_events WHERE event_id=$1")
            .bind(event.as_uuid())
            .fetch_one(&f.pool)
            .await
            .unwrap();
    for case in ["marker", "before"] {
        let mut request = serde_json::to_value(original.request()).unwrap();
        if case == "marker" {
            request["request"]["clone"]["request_marker"] = json!(Uuid::now_v7());
        } else {
            request["request"]["expected_before"]["config"]["digest"] =
                json!("substituted-before-digest");
        }
        let request: pve_port::ProvisioningMutationRequestV1 =
            serde_json::from_value(request).unwrap();
        let hash = request.request_digest().unwrap();
        let mut changed = decision.clone();
        changed["detail"]["request_sha256"] = json!(hash);
        let mut tx = f.pool.begin().await.unwrap();
        sqlx::raw_sql("ALTER TABLE rust_controller.osdeploy_pve_dispatches DISABLE TRIGGER osdeploy_no_mutation; ALTER TABLE rust_controller.osdeploy_decisions DISABLE TRIGGER osdeploy_no_mutation").execute(&mut *tx).await.unwrap();
        sqlx::query("UPDATE rust_controller.osdeploy_pve_dispatches SET request_sha256=$2,request_canonical_json=$3 WHERE operation_id=$1")
            .bind(h.operation.as_uuid()).bind(&hash).bind(String::from_utf8(event_journal::canonical_json_bytes(&serde_json::to_value(request).unwrap()).unwrap()).unwrap()).execute(&mut *tx).await.unwrap();
        sqlx::query("UPDATE rust_controller.osdeploy_decisions SET payload_canonical_json=$2 WHERE event_id=$1")
            .bind(event.as_uuid()).bind(String::from_utf8(event_journal::canonical_json_bytes(&changed).unwrap()).unwrap()).execute(&mut *tx).await.unwrap();
        sqlx::query("UPDATE rust_controller.journal_events SET payload=$2,payload_digest=$3 WHERE event_id=$1")
            .bind(event.as_uuid()).bind(&changed).bind(event_journal::payload_digest(&changed).unwrap()).execute(&mut *tx).await.unwrap();
        sqlx::raw_sql("ALTER TABLE rust_controller.osdeploy_pve_dispatches ENABLE TRIGGER osdeploy_no_mutation; ALTER TABLE rust_controller.osdeploy_decisions ENABLE TRIGGER osdeploy_no_mutation").execute(&mut *tx).await.unwrap();
        tx.commit().await.unwrap();
        assert!(
            matches!(
                f.store.load_osdeploy_operation(h.operation).await,
                Err(postgres_store::OsDeployExecutionError::Validation)
            ),
            "{case}"
        );
    }
}

#[tokio::test]
async fn activation_creates_one_attempt_with_policy_deadline() {
    let f = osdeploy_support::Fixture::new().await;
    let p = osdeploy_support::plan();
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &p)
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let g = f
        .scheduler()
        .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(g.attempt_number(), 1);
    assert_eq!((*g.deadline_at() - *g.acquired_at()).num_seconds(), 300);
    let s = f.store.load_osdeploy_operation(op).await.unwrap();
    assert_eq!(s.attempt_id(), Some(g.attempt_id()));
    assert_eq!(s.state(), controller_domain::ExecutionState::Leased);
    assert!(
        f.other_scheduler()
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .is_none()
    );
    f.scheduler()
        .start_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    let starts: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='attempt_started'")
        .bind(op.as_uuid()).fetch_one(&f.pool).await.unwrap();
    assert_eq!(starts, 1);
}

use osdeploy_support::{Fixture, plan};
use serde_json::{Value, json};
use sqlx::{PgConnection, postgres::PgPoolOptions};
use std::time::Duration;
use uuid::Uuid;

const TABLES: [&str; 9] = [
    "osdeploy_decisions",
    "osdeploy_deadlines",
    "osdeploy_attempt_bindings",
    "osdeploy_lease_epochs",
    "osdeploy_pve_evidence",
    "osdeploy_pve_dispatches",
    "osdeploy_pve_receipts",
    "osdeploy_run_cancellations",
    "osdeploy_schedule_projection",
];

#[tokio::test]
async fn lifecycle_same_operation_race_and_start_replay_are_atomic() {
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let first = f.scheduler();
    let second = f.other_scheduler();
    let (a, b) = tokio::time::timeout(Duration::from_secs(10), async {
        tokio::join!(
            first.claim_osdeploy_bound(op, ids.workflow_sha256(), 1),
            second.claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        )
    })
    .await
    .unwrap();
    let (g, scheduler) = match (a.unwrap(), b.unwrap()) {
        (Some(g), None) => (g, first),
        (None, Some(g)) => (g, second),
        _ => panic!("one claimant must win"),
    };
    let other_pool_same_owner = postgres_store::Scheduler::new(
        f.other.clone(),
        postgres_store::ExecutorKind::Rust,
        1,
        g.worker_id(),
    )
    .unwrap();
    let (a, b) = tokio::time::timeout(Duration::from_secs(10), async {
        tokio::join!(
            scheduler.start_osdeploy_bound(&g, ids.workflow_sha256()),
            other_pool_same_owner.start_osdeploy_bound(&g, ids.workflow_sha256())
        )
    })
    .await
    .unwrap();
    assert_eq!(a.unwrap(), controller_domain::ExecutionState::Running);
    assert_eq!(b.unwrap(), controller_domain::ExecutionState::Running);
    let before = f.snapshot().await;
    scheduler
        .start_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(f.snapshot().await, before);
    assert_eq!(before["attempts"].as_array().unwrap().len(), 1);
    assert_eq!(before["osdeploy_lease_epochs"].as_array().unwrap().len(), 1);
    let chain: Vec<(i64,String,chrono::DateTime<chrono::Utc>,Uuid)> = sqlx::query_as("SELECT aggregate_revision,event_kind,observed_at,attempt_id FROM rust_controller.journal_events WHERE operation_id=$1 ORDER BY aggregate_revision")
        .bind(op.as_uuid()).fetch_all(&f.pool).await.unwrap();
    assert_eq!(
        chain
            .iter()
            .map(|r| (r.0, r.1.as_str()))
            .collect::<Vec<_>>(),
        vec![
            (1, "decision_recorded"),
            (2, "decision_recorded"),
            (3, "execution_state_changed"),
            (4, "attempt_started"),
            (5, "decision_recorded"),
            (6, "execution_state_changed")
        ]
    );
    assert_eq!(chain[3].2, chain[4].2);
    assert_eq!(chain[4].2, chain[5].2);
    assert!(chain.iter().all(|r| r.3 == g.attempt_id().as_uuid()));
    assert_eq!(
        f.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .revision(),
        6
    );
    let secret_in_history: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.journal_events WHERE operation_id=$1 AND payload::text LIKE '%' || $2 || '%') OR EXISTS(SELECT 1 FROM rust_controller.outbox WHERE operation_id=$1 AND payload::text LIKE '%' || $2 || '%')")
        .bind(op.as_uuid()).bind(g.lease_token().to_string()).fetch_one(&f.pool).await.unwrap();
    assert!(!secret_in_history);
}

fn other_lifecycle_plan() -> osdeploy_adapter::OsDeployPlanV1 {
    osdeploy_support::altered(|v| {
        v["vm"]["target_vmid"] = json!(102);
        v["vm"]["uuid"] = json!("33333333-3333-4333-8333-333333333302");
        v["vm"]["mac"] = json!("02:00:00:00:01:02");
        v["names"]["requested_name"] = json!("Another");
        v["names"]["windows_name"] = json!("Another");
        v["names"]["expected_agent_id"] = json!("agent-another");
    })
}

#[tokio::test]
async fn lifecycle_last_cap_race_counts_old_generation_leases() {
    let f = Fixture::new().await;
    let first = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let second = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &other_lifecycle_plan())
        .await
        .unwrap();
    let a = f.scheduler();
    let b = f.other_scheduler();
    let (left, right) = tokio::time::timeout(Duration::from_secs(10), async {
        tokio::join!(
            a.claim_osdeploy_bound(
                first.operation(osdeploy_adapter::OsDeployStage::Clone),
                first.workflow_sha256(),
                1
            ),
            b.claim_osdeploy_bound(
                second.operation(osdeploy_adapter::OsDeployStage::Clone),
                second.workflow_sha256(),
                1
            )
        )
    })
    .await
    .unwrap();
    let (loser, winner_grant) = match (left.unwrap(), right.unwrap()) {
        (Some(g), None) => (second, g),
        (None, Some(g)) => (first, g),
        _ => panic!("last cap slot must have one owner"),
    };
    sqlx::query("UPDATE rust_controller.orchestration_authority SET generation=2")
        .execute(&f.pool)
        .await
        .unwrap();
    let current = postgres_store::Scheduler::new(
        f.other.clone(),
        postgres_store::ExecutorKind::Rust,
        2,
        "generation-two",
    )
    .unwrap();
    let before = f.snapshot().await;
    assert!(
        current
            .claim_osdeploy_bound(
                loser.operation(osdeploy_adapter::OsDeployStage::Clone),
                loser.workflow_sha256(),
                1
            )
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(f.snapshot().await, before);
    assert_eq!(winner_grant.generation(), 1);
}

#[tokio::test]
async fn lifecycle_heartbeat_refreshes_epoch_without_extending_scope() {
    let f = Fixture::new().await;
    let p = osdeploy_support::altered(|v| {
        v["policy"]["mutation_seconds"] = json!(12);
        v["policy"]["evidence_freshness_seconds"] = json!(1);
    });
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &p)
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let scheduler = f.scheduler();
    let g = scheduler
        .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    scheduler
        .start_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(g.lease_expires_at(), g.deadline_at());
    let before = f.snapshot().await;
    let early = scheduler
        .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(early.revision(), 6);
    assert_eq!(f.snapshot().await, before);
    tokio::time::timeout(Duration::from_secs(13),sqlx::query("SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz + interval '10.05 seconds' - clock_timestamp()))))")
        .bind(g.acquired_at()).execute(&f.pool)).await.unwrap().unwrap();
    let refreshed = scheduler
        .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(refreshed.revision(), 7);
    assert_eq!(refreshed.grant().deadline_at(), g.deadline_at());
    assert_eq!(refreshed.grant().acquired_at(), g.acquired_at());
    assert_eq!(refreshed.grant().attempt_id(), g.attempt_id());
    assert_eq!(refreshed.grant().lease_token(), g.lease_token());
    assert_eq!(refreshed.grant().lease_expires_at(), g.deadline_at());
    assert!(refreshed.grant().heartbeat_at() > g.heartbeat_at());
    assert!(refreshed.remaining() < Duration::from_secs(2));
    let before = f.snapshot().await;
    let observed = scheduler
        .continuation_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(observed.grant(), refreshed.grant());
    assert!(observed.checked_at() >= refreshed.checked_at());
    assert_eq!(f.snapshot().await, before);
    assert_eq!(
        f.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .revision(),
        7
    );
}

#[tokio::test]
async fn lifecycle_heartbeat_extends_only_the_existing_short_lease() {
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let scheduler = f.scheduler();
    let g = scheduler
        .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    tokio::time::timeout(Duration::from_secs(13),sqlx::query("SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz + interval '10.05 seconds' - clock_timestamp()))))")
        .bind(g.acquired_at()).execute(&f.pool)).await.unwrap().unwrap();
    let refreshed = scheduler
        .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(refreshed.revision(), 4);
    assert!(*refreshed.grant().lease_expires_at() > *g.lease_expires_at());
    assert_eq!(
        (*refreshed.grant().lease_expires_at() - *refreshed.grant().heartbeat_at()).num_seconds(),
        30
    );
    assert_eq!(refreshed.grant().deadline_at(), g.deadline_at());
    assert_eq!(refreshed.grant().attempt_id(), g.attempt_id());
    assert_eq!(refreshed.grant().acquired_at(), g.acquired_at());
    let before = f.snapshot().await;
    let observed = scheduler
        .continuation_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(observed.grant(), refreshed.grant());
    assert!(observed.checked_at() >= refreshed.checked_at());
    assert!(
        f.snapshot().await == before,
        "continuation changed durable state"
    );
    scheduler
        .start_osdeploy_bound(&g, ids.workflow_sha256())
        .await
        .unwrap();
    assert_eq!(
        f.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .revision(),
        7
    );
}

#[tokio::test]
async fn lifecycle_fences_workers_hashes_generation_and_disabled_stages_without_writes() {
    use postgres_store::OsDeployExecutionError as E;
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let before = f.snapshot().await;
    assert!(matches!(
        f.scheduler()
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 0)
            .await,
        Err(E::Validation)
    ));
    assert!(matches!(
        f.scheduler().claim_osdeploy_bound(op, "bad", 1).await,
        Err(E::Validation)
    ));
    assert!(matches!(
        f.scheduler()
            .claim_osdeploy_bound(op, &"a".repeat(64), 1)
            .await,
        Err(E::FenceLost)
    ));
    for stage in osdeploy_adapter::OsDeployStage::ALL.into_iter().skip(3) {
        assert!(
            matches!(
                f.scheduler()
                    .claim_osdeploy_bound(ids.operation(stage), ids.workflow_sha256(), 1)
                    .await,
                Err(E::CapabilityUnavailable)
            ),
            "{stage:?}"
        );
    }
    for stage in [
        osdeploy_adapter::OsDeployStage::DiskCapacity,
        osdeploy_adapter::OsDeployStage::ConfigurePe,
    ] {
        assert!(
            f.scheduler()
                .claim_osdeploy_bound(ids.operation(stage), ids.workflow_sha256(), 1)
                .await
                .unwrap()
                .is_none()
        );
    }
    assert_eq!(f.snapshot().await, before);
    let g = f
        .scheduler()
        .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    let before = f.snapshot().await;
    assert!(matches!(
        f.other_scheduler()
            .start_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.other_scheduler()
            .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.other_scheduler()
            .continuation_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.scheduler()
            .start_osdeploy_bound(&g, &"a".repeat(64))
            .await,
        Err(E::FenceLost)
    ));
    assert_eq!(f.snapshot().await, before);
    sqlx::query("UPDATE rust_controller.orchestration_authority SET generation=2")
        .execute(&f.pool)
        .await
        .unwrap();
    let before = f.snapshot().await;
    assert!(matches!(
        f.scheduler().osdeploy_authority_snapshot().await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.scheduler()
            .start_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.scheduler()
            .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        f.scheduler()
            .continuation_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(E::FenceLost)
    ));
    assert_eq!(f.snapshot().await, before);
}

#[tokio::test]
async fn lifecycle_activation_rolls_back_at_every_write_and_commit_boundary() {
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    sqlx::raw_sql("CREATE SEQUENCE lifecycle_write_number; CREATE TABLE lifecycle_fault(target bigint); INSERT INTO lifecycle_fault VALUES(0); CREATE FUNCTION lifecycle_fail_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF nextval('lifecycle_write_number')=(SELECT target FROM lifecycle_fault) THEN RAISE EXCEPTION 'owned_lifecycle_fault'; END IF; RETURN NEW; END $$;").execute(&f.pool).await.unwrap();
    for table in [
        "attempts",
        "journal_events",
        "outbox",
        "osdeploy_decisions",
        "operations",
        "operation_projection",
        "osdeploy_deadlines",
        "osdeploy_attempt_bindings",
        "osdeploy_lease_epochs",
        "worker_leases",
    ] {
        sqlx::query(&format!("CREATE TRIGGER lifecycle_write_fault BEFORE INSERT OR UPDATE ON rust_controller.{table} FOR EACH ROW EXECUTE FUNCTION lifecycle_fail_write()"))
            .execute(&f.pool).await.unwrap();
    }
    let before = f.snapshot().await;
    // Projection UPSERT fires its INSERT and UPDATE row triggers, so the two
    // decision projections and final state projection each have two boundaries.
    for boundary in 1..=22_i64 {
        sqlx::query("UPDATE lifecycle_fault SET target=$1")
            .bind(boundary)
            .execute(&f.pool)
            .await
            .unwrap();
        sqlx::query("SELECT setval('lifecycle_write_number',1,false)")
            .execute(&f.pool)
            .await
            .unwrap();
        assert!(
            matches!(
                f.scheduler()
                    .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
                    .await,
                Err(postgres_store::OsDeployExecutionError::StorageUnavailable)
            ),
            "write boundary {boundary}"
        );
        assert_eq!(f.snapshot().await, before, "write boundary {boundary}");
    }
    sqlx::query("UPDATE lifecycle_fault SET target=0")
        .execute(&f.pool)
        .await
        .unwrap();
    sqlx::raw_sql("CREATE FUNCTION lifecycle_fail_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned_lifecycle_commit_fault'; END $$; CREATE CONSTRAINT TRIGGER lifecycle_commit_fault AFTER INSERT ON rust_controller.worker_leases DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lifecycle_fail_commit();").execute(&f.pool).await.unwrap();
    assert!(matches!(
        f.scheduler()
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
            .await,
        Err(postgres_store::OsDeployExecutionError::StorageUnavailable)
    ));
    assert_eq!(f.snapshot().await, before);
    sqlx::query("DROP TRIGGER lifecycle_commit_fault ON rust_controller.worker_leases")
        .execute(&f.pool)
        .await
        .unwrap();
    assert!(
        f.scheduler()
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .is_some()
    );
    f.store.load_osdeploy_operation(op).await.unwrap();
}

#[tokio::test]
async fn lifecycle_final_clock_check_rolls_back_expired_activation_and_start() {
    let f = Fixture::new().await;
    let p = osdeploy_support::altered(|v| {
        v["policy"]["mutation_seconds"] = json!(1);
        v["policy"]["evidence_freshness_seconds"] = json!(1);
    });
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &p)
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    sqlx::raw_sql("CREATE FUNCTION lifecycle_delay_state() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.event_kind='execution_state_changed' THEN PERFORM pg_sleep(1.05); END IF; RETURN NEW; END $$; CREATE TRIGGER lifecycle_delay BEFORE INSERT ON rust_controller.journal_events FOR EACH ROW EXECUTE FUNCTION lifecycle_delay_state();").execute(&f.pool).await.unwrap();
    let before = f.snapshot().await;
    assert!(matches!(
        f.scheduler()
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert_eq!(f.snapshot().await, before);
    sqlx::query("ALTER TABLE rust_controller.journal_events DISABLE TRIGGER lifecycle_delay")
        .execute(&f.pool)
        .await
        .unwrap();
    let g = f
        .scheduler()
        .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    sqlx::query("ALTER TABLE rust_controller.journal_events ENABLE TRIGGER lifecycle_delay")
        .execute(&f.pool)
        .await
        .unwrap();
    let before = f.snapshot().await;
    assert!(matches!(
        f.scheduler()
            .start_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert_eq!(f.snapshot().await, before);
    assert!(matches!(
        f.scheduler()
            .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert!(matches!(
        f.scheduler()
            .continuation_osdeploy_bound(&g, ids.workflow_sha256())
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert_eq!(f.snapshot().await, before);
}

#[tokio::test]
async fn lifecycle_historical_generic_osdeploy_cannot_be_adopted() {
    use controller_domain::{
        CommandEnvelope, OperationId, RunId, SemanticOperationKey, WorkflowKind,
    };
    use postgres_store::OsDeployExecutionError as E;
    let f = Fixture::new().await;
    let op = OperationId::new();
    let command = CommandEnvelope::new(
        "historical-lifecycle",
        SemanticOperationKey::new(
            WorkflowKind::SyntheticLongSleep,
            RunId::new(),
            "historical",
            1,
        )
        .unwrap(),
        "a".repeat(64),
    )
    .unwrap();
    f.store.append_command(op, &command).await.unwrap();
    let g = f
        .scheduler()
        .claim_next(WorkflowKind::SyntheticLongSleep, 1)
        .await
        .unwrap()
        .unwrap();
    sqlx::query(
        "UPDATE rust_controller.operations SET workflow_kind='os_deploy' WHERE operation_id=$1",
    )
    .bind(op.as_uuid())
    .execute(&f.pool)
    .await
    .unwrap();
    let before = f.snapshot().await;
    assert!(matches!(
        f.scheduler()
            .claim_osdeploy_bound(op, &"a".repeat(64), 1)
            .await,
        Err(E::Validation)
    ));
    assert!(matches!(
        f.scheduler()
            .start_osdeploy_bound(&g, &"a".repeat(64))
            .await,
        Err(E::Validation)
    ));
    assert!(matches!(
        f.scheduler()
            .heartbeat_osdeploy_bound(&g, &"a".repeat(64))
            .await,
        Err(E::Validation)
    ));
    assert!(matches!(
        f.scheduler()
            .continuation_osdeploy_bound(&g, &"a".repeat(64))
            .await,
        Err(E::Validation)
    ));
    assert!(
        f.snapshot().await == before,
        "historical execution rejection wrote data"
    );
}

#[tokio::test]
async fn unactivated_reload_rejects_an_unbound_attempt() {
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state) VALUES($1,$2,1,'pending')")
        .bind(controller_domain::AttemptId::new().as_uuid()).bind(op.as_uuid())
        .execute(&f.pool).await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(op).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

// Internally consistent SQL history exercises observational reload only. These
// rows never provide successful predecessors or enter a scheduler/send API.
struct ReloadHistory {
    operation: controller_domain::OperationId,
    attempt: controller_domain::AttemptId,
    run: controller_domain::RunId,
    activation: controller_domain::EventId,
    acquisition: controller_domain::EventId,
    at: chrono::DateTime<chrono::Utc>,
    deadline: chrono::DateTime<chrono::Utc>,
    workflow: String,
    stage: String,
}
impl ReloadHistory {
    async fn new(f: &Fixture) -> Self {
        Self::with_plan(f, plan()).await
    }
    async fn with_plan(f: &Fixture, p: osdeploy_adapter::OsDeployPlanV1) -> Self {
        let run = controller_domain::RunId::new();
        let ids = f.store.enqueue_osdeploy(run, &p).await.unwrap();
        let operation = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
        let registration = f.store.load_osdeploy_registration(run).await.unwrap();
        let at = "2026-09-05T12:00:00Z"
            .parse::<chrono::DateTime<chrono::Utc>>()
            .unwrap();
        let s = Self {
            operation,
            attempt: controller_domain::AttemptId::new(),
            run,
            activation: controller_domain::EventId::new(),
            acquisition: controller_domain::EventId::new(),
            at,
            deadline: at + chrono::Duration::seconds(i64::from(p.policy().mutation_seconds())),
            workflow: ids.workflow_sha256().to_owned(),
            stage: registration
                .stage(osdeploy_adapter::OsDeployStage::Clone)
                .fingerprint()
                .unwrap(),
        };
        let mut tx = f.pool.begin().await.unwrap();
        sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state,started_at,deadline_at) VALUES($1,$2,1,'leased',$3,$4)")
            .bind(s.attempt.as_uuid()).bind(operation.as_uuid()).bind(s.at).bind(s.deadline).execute(&mut *tx).await.unwrap();
        let activation = s.envelope(
            "stage_activated",
            0,
            Value::Null,
            json!({"scope_key":"mutation_clone",
            "anchor_operation_id":operation,"anchor_event_id":s.activation,"opened_at":s.at,
            "budget_seconds":p.policy().mutation_seconds(),"deadline_at":s.deadline,
            "predecessor_operation_id":null,"predecessor_decision_event_id":null}),
        );
        s.decision(&mut tx, s.activation, activation).await;
        insert_row(
            &mut tx,
            "osdeploy_deadlines",
            &json!({"run_id":run,"scope_key":"mutation_clone",
            "anchor_operation_id":operation,"anchor_event_id":s.activation,"opened_at":s.at,
            "budget_seconds":p.policy().mutation_seconds(),"deadline_at":s.deadline}),
        )
        .await
        .unwrap();
        insert_row(
            &mut tx,
            "osdeploy_attempt_bindings",
            &json!({"operation_id":operation,"run_id":run,
            "attempt_id":s.attempt,"scope_key":"mutation_clone","activation_event_id":s.activation,
            "activated_at":s.at,"deadline_at":s.deadline,"activation_mode":"leased"}),
        )
        .await
        .unwrap();
        let token = Uuid::now_v7().to_string();
        let token_hash: String =
            sqlx::query_scalar("SELECT encode(sha256(convert_to($1,'UTF8')),'hex')")
                .bind(&token)
                .fetch_one(&mut *tx)
                .await
                .unwrap();
        s.decision(&mut tx,s.acquisition,s.envelope("lease_acquired",1,Value::Null,json!({"purpose":"initial_evaluation",
            "acquisition_event_id":s.acquisition,"token_sha256":token_hash,"worker_id":"reload-only-worker",
            "acquired_at":s.at,"expires_at":s.at+chrono::Duration::seconds(30),"deadline_at":s.deadline,
            "prior_schedule_event_id":null}))).await;
        insert_row(&mut tx,"osdeploy_lease_epochs",&json!({"acquisition_event_id":s.acquisition,"operation_id":operation,
            "run_id":run,"attempt_id":s.attempt,"executor_kind":"rust","generation":1,"worker_id":"reload-only-worker",
            "lease_token_sha256":token_hash,"acquired_at":s.at,"initial_expires_at":s.at+chrono::Duration::seconds(30),
            "deadline_at":s.deadline,"purpose":"initial_evaluation"})).await.unwrap();
        s.event(
            &mut tx,
            controller_domain::EventId::new(),
            3,
            "execution_state_changed",
            Some("leased"),
            json!({"state":"leased","decision_event_id":s.acquisition}),
            s.at,
        )
        .await;
        sqlx::query("INSERT INTO rust_controller.worker_leases(operation_id,attempt_id,executor_kind,generation,worker_id,lease_token,acquired_at,heartbeat_at,lease_expires_at,deadline_at) VALUES($1,$2,'rust',1,'reload-only-worker',$6,$3,$3,$4,$5)")
            .bind(operation.as_uuid()).bind(s.attempt.as_uuid()).bind(s.at).bind(s.at+chrono::Duration::seconds(30)).bind(s.deadline).bind(token).execute(&mut *tx).await.unwrap();
        tx.commit().await.unwrap();
        s
    }
    fn envelope(&self, action: &str, before: i64, resolution: Value, detail: Value) -> Value {
        json!({"contract_version":1,"action":action,"run_id":self.run,"operation_id":self.operation,
            "workflow_sha256":self.workflow,"stage_sha256":self.stage,"attempt_id":self.attempt,
            "generation":1,"before_revision":before,"evaluated_at":self.at,"resolution":resolution,"detail":detail})
    }
    async fn decision(
        &self,
        tx: &mut PgConnection,
        event: controller_domain::EventId,
        payload: Value,
    ) {
        let revision = payload["before_revision"].as_i64().unwrap() + 1;
        let at = serde_json::from_value(payload["evaluated_at"].clone()).unwrap();
        self.event(
            tx,
            event,
            revision,
            "decision_recorded",
            None,
            payload.clone(),
            at,
        )
        .await;
        let canonical =
            String::from_utf8(event_journal::canonical_json_bytes(&payload).unwrap()).unwrap();
        insert_row(tx,"osdeploy_decisions",&json!({"operation_id":self.operation,"decision_revision":revision,
            "event_id":event,"run_id":self.run,"attempt_id":self.attempt,"action":payload["action"],
            "resolution":payload["resolution"],"workflow_sha256":self.workflow,"stage_sha256":self.stage,
            "generation":1,"evaluated_at":at,"payload_canonical_json":canonical})).await.unwrap();
    }
    #[allow(clippy::too_many_arguments)]
    async fn event(
        &self,
        tx: &mut PgConnection,
        event: controller_domain::EventId,
        revision: i64,
        kind: &str,
        state: Option<&str>,
        payload: Value,
        at: chrono::DateTime<chrono::Utc>,
    ) {
        sqlx::query("INSERT INTO rust_controller.journal_events(event_id,operation_id,attempt_id,aggregate_revision,semantic_key,payload_digest,event_kind,execution_state,payload,observed_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)")
            .bind(event.as_uuid()).bind(self.operation.as_uuid()).bind(self.attempt.as_uuid()).bind(revision)
            .bind(format!("reload-only:{}",event.as_uuid())).bind(event_journal::payload_digest(&payload).unwrap()).bind(kind).bind(state).bind(payload).bind(at).execute(&mut *tx).await.unwrap();
        sqlx::query("UPDATE rust_controller.operations SET revision=$2,state=coalesce($3,state) WHERE operation_id=$1")
            .bind(self.operation.as_uuid()).bind(revision).bind(state).execute(&mut *tx).await.unwrap();
        if let Some(state) = state {
            sqlx::query("UPDATE rust_controller.attempts SET state=$2,completed_at=CASE WHEN $2 IN ('satisfied','failed','blocked','unknown','conflicted') THEN $3 ELSE NULL END WHERE attempt_id=$1")
            .bind(self.attempt.as_uuid()).bind(state).bind(at).execute(&mut *tx).await.unwrap();
        }
    }

    async fn started(&self, f: &Fixture) {
        let mut tx = f.pool.begin().await.unwrap();
        self.event(
            &mut tx,
            controller_domain::EventId::new(),
            4,
            "attempt_started",
            None,
            json!({"phase":"mutation_started"}),
            self.at,
        )
        .await;
        let decision = controller_domain::EventId::new();
        self.decision(
            &mut tx,
            decision,
            self.envelope(
                "evaluation_started",
                4,
                Value::Null,
                json!({"lease_acquisition_event_id":self.acquisition,"activity":"preflight_read"}),
            ),
        )
        .await;
        self.event(
            &mut tx,
            controller_domain::EventId::new(),
            6,
            "execution_state_changed",
            Some("running"),
            json!({"state":"running","decision_event_id":decision}),
            self.at,
        )
        .await;
        tx.commit().await.unwrap();
    }
    async fn dispatched(
        &self,
        f: &Fixture,
        source: pve_port::ProvisioningVmConfigV1,
    ) -> (
        controller_domain::EventId,
        controller_domain::EventId,
        pve_port::ProvisioningDispatchV1,
    ) {
        use pve_port::*;
        self.started(f).await;
        let reg = f.store.load_osdeploy_registration(self.run).await.unwrap();
        let p = reg
            .stage(osdeploy_adapter::OsDeployStage::Clone)
            .pve()
            .unwrap()
            .clone();
        let binding = ProvisioningBindingV1::new(
            self.run,
            self.operation,
            self.attempt,
            &self.workflow,
            &p,
            6,
        )
        .unwrap();
        let power = VmPowerStatus::from_wire(
            p.expected().vm().node().clone(),
            p.expected().vm().source_vmid(),
            json!({"vmid":900,"status":"stopped","locked":0}),
            self.at,
        )
        .unwrap();
        // Structural corruption fixture only: complete this owned historical
        // world at its original time. It is never a successful predecessor.
        let node = p.expected().vm().node().clone();
        let media = [
            p.expected().deployment_iso_volid(),
            p.expected().driver_iso_volid(),
        ]
        .into_iter()
        .map(|volid| {
            NativeRead::new(
                self.at,
                Ok(ProvisioningMediaInventoryV1::new(
                    node.clone(),
                    StorageName::parse(volid.split_once(':').unwrap().0).unwrap(),
                    vec![volid.to_owned()],
                    ProvisioningCoverageV1::Complete,
                    self.at,
                )
                .unwrap()),
            )
        })
        .collect();
        let facts = ProvisioningEvidenceV1::new(ProvisioningEvidenceInputV1 {
            binding: binding.clone(),
            plan: p.clone(),
            source: NativeEvidenceSource::FakePve,
            collected_at: self.at,
            node: Some(NativeRead::new(self.at, Ok(NodeStatus::from_wire(node.clone(), json!({"uptime":100}), self.at).unwrap()))),
            storage: Some(NativeRead::new(self.at, Ok(StorageStatus::from_wire(node.clone(),
                p.expected().vm().storage().clone(), json!({"active":1,"enabled":1,"content":"images","avail":999999999999_u64}), self.at).unwrap()))),
            bridges: Some(NativeRead::new(self.at, Ok(BridgeInventory::from_wire(node.clone(),
                json!([{"type":"bridge","iface":"vmbr0","active":1}]), self.at).unwrap()))),
            inventory: Some(NativeRead::new(self.at, Ok(ClusterVmInventory::from_wire(
                json!([{"node":"node-a","vmid":900,"name":"blank-template","template":1,"status":"stopped","type":"qemu"}]), self.at).unwrap()))),
            inventory_coverage: ProvisioningCoverageV1::Complete,
            identities: vec![ProvisioningIdentityReadV1::new(node.clone(), source.vmid(),
                NativeRead::new(self.at, Ok(ProvisioningIdentitySnapshotV1::from_provisioning(&source)))).unwrap()],
            source_config: Some(NativeRead::new(self.at, Ok(source.clone()))),
            source_power: Some(NativeRead::new(self.at, Ok(power.clone()))),
            target_config: Some(NativeRead::new(self.at, Err(PveReadError::NotFound))),
            target_power: Some(NativeRead::new(self.at, Err(PveReadError::NotFound))),
            media,
            qga: None,
            task: None,
            receipt: None,
        })
        .unwrap();
        let clone: CloneRequest = serde_json::from_value(json!({"vm":p.expected().vm(),
            "operation_id":self.operation,"request_marker":self.operation.as_uuid()}))
        .unwrap();
        let request = ProvisioningMutationRequestV1::Clone(
            CloneProvisioningRequestV1::new(
                binding,
                p.clone(),
                clone,
                ProvisioningBeforeStateV1::new(source, power).unwrap(),
                self.at,
                30,
            )
            .unwrap(),
        );
        let evidence = controller_domain::EventId::new();
        let dispatch = controller_domain::EventId::new();
        let d = ProvisioningDispatchV1::new(ProvisioningDispatchInputV1 {
            request: request.clone(),
            source: NativeEvidenceSource::FakePve,
            preflight_event_id: evidence,
            original_generation: 1,
            dispatch_revision: 8,
            dispatched_at: self.at,
        })
        .unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        let payload = serde_json::to_value(&facts).unwrap();
        self.event(
            &mut tx,
            evidence,
            7,
            "evidence_recorded",
            None,
            payload.clone(),
            self.at,
        )
        .await;
        insert_row(&mut tx,"osdeploy_pve_evidence",&json!({"event_id":evidence,"operation_id":self.operation,"run_id":self.run,
            "attempt_id":self.attempt,"evidence_revision":7,"evidence_sha256":event_journal::payload_digest(&payload).unwrap(),"source":"fake_pve",
            "evidence_canonical_json":String::from_utf8(event_journal::canonical_json_bytes(&payload).unwrap()).unwrap()})).await.unwrap();
        self.decision(&mut tx,dispatch,self.envelope("pve_dispatch_committed",7,json!("ready"),json!({"preflight_event_id":evidence,
            "pve_plan_sha256":p.fingerprint().unwrap(),"request_sha256":request.request_digest().unwrap(),"dispatched_at":self.at,
            "lease_acquisition_event_id":self.acquisition}))).await;
        insert_row(&mut tx,"osdeploy_pve_dispatches",&json!({"operation_id":self.operation,"run_id":self.run,"attempt_id":self.attempt,
            "dispatch_event_id":dispatch,"dispatch_revision":8,"preflight_event_id":evidence,"workflow_sha256":self.workflow,
            "pve_plan_sha256":p.fingerprint().unwrap(),"request_sha256":request.request_digest().unwrap(),
            "request_canonical_json":String::from_utf8(event_journal::canonical_json_bytes(&serde_json::to_value(request).unwrap()).unwrap()).unwrap(),
            "source":"fake_pve","original_generation":1,"dispatched_at":self.at,"lease_acquisition_event_id":self.acquisition})).await.unwrap();
        tx.commit().await.unwrap();
        (dispatch, evidence, d)
    }
}

fn reload_source() -> pve_port::ProvisioningVmConfigV1 {
    pve_port::ProvisioningVmConfigV1::from_wire(pve_port::NodeName::parse("node-a").unwrap(),pve_port::Vmid::new(900).unwrap(),pve_port::NativeEvidenceSource::FakePve,
        json!({"node":"node-a","vmid":900,"digest":"reload-template","name":"blank-template","cores":2,"memory":2048,
            "scsi0":"disk-store:vm-900-disk-0,size=80G","smbios1":"uuid=33333333-3333-4333-8333-333333333390",
            "net0":"virtio=02:00:00:00:09:00,bridge=vmbr0,firewall=0","bios":"seabios","cpu":"host","balloon":0,
            "agent":"enabled=0,type=virtio","boot":"order=scsi0","template":1}),"2026-09-05T12:00:00Z".parse().unwrap()).unwrap()
}

#[tokio::test]
async fn first_start_reload_rejects_timestamp_only_corruption_and_restores() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    h.started(&f).await;
    assert_eq!(
        f.store
            .load_osdeploy_operation(h.operation)
            .await
            .unwrap()
            .state(),
        controller_domain::ExecutionState::Running
    );
    sqlx::query("UPDATE rust_controller.journal_events SET observed_at=$2 WHERE operation_id=$1 AND event_kind='attempt_started'")
        .bind(h.operation.as_uuid()).bind(h.at+chrono::Duration::seconds(1)).execute(&f.pool).await.unwrap();
    assert!(
        matches!(
            f.store.load_osdeploy_operation(h.operation).await,
            Err(postgres_store::OsDeployExecutionError::Validation)
        ),
        "timestamp-only first-start corruption was accepted"
    );
    sqlx::query("UPDATE rust_controller.journal_events SET observed_at=$2 WHERE operation_id=$1 AND event_kind='attempt_started'")
        .bind(h.operation.as_uuid()).bind(h.at).execute(&f.pool).await.unwrap();
    assert_eq!(
        f.store
            .load_osdeploy_operation(h.operation)
            .await
            .unwrap()
            .state(),
        controller_domain::ExecutionState::Running
    );
}

#[tokio::test]
async fn first_start_reload_rejects_interleaved_history() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    let mut tx = f.pool.begin().await.unwrap();
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        4,
        "attempt_started",
        None,
        json!({"phase":"mutation_started"}),
        h.at,
    )
    .await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        5,
        "evidence_recorded",
        None,
        json!({"unrelated":"observational-only"}),
        h.at,
    )
    .await;
    let evaluation = controller_domain::EventId::new();
    h.decision(
        &mut tx,
        evaluation,
        h.envelope(
            "evaluation_started",
            5,
            Value::Null,
            json!({"lease_acquisition_event_id":h.acquisition,"activity":"preflight_read"}),
        ),
    )
    .await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        7,
        "execution_state_changed",
        Some("running"),
        json!({"state":"running","decision_event_id":evaluation}),
        h.at,
    )
    .await;
    tx.commit().await.unwrap();
    assert!(
        matches!(
            f.store.load_osdeploy_operation(h.operation).await,
            Err(postgres_store::OsDeployExecutionError::Validation)
        ),
        "interleaved first-start transaction was accepted"
    );
}

#[tokio::test]
async fn ordinary_lease_reload_park_resume_and_later_start() {
    use pve_port::*;
    let f = Fixture::new().await;
    let source = reload_source();
    let p = osdeploy_support::altered(|v| {
        v["template_config_sha256"] = json!(source.template_fingerprint().unwrap())
    });
    let h = ReloadHistory::with_plan(&f, p).await;
    let (dispatch_event, preflight, dispatch) = h.dispatched(&f, source).await;
    let old_lease: Value = sqlx::query_scalar(
        "SELECT to_jsonb(l) FROM rust_controller.worker_leases l WHERE operation_id=$1",
    )
    .bind(h.operation.as_uuid())
    .fetch_one(&f.pool)
    .await
    .unwrap();
    // This remains a structural lease-history fixture, not a writer proof.
    // A task-running park needs its original receipt and actual task evidence.
    let receipt_event = controller_domain::EventId::new();
    let evidence = controller_domain::EventId::new();
    let upid = Upid::parse("UPID:node-a:00000001:00000001:00000001:qmclone:900:root@pam:").unwrap();
    let receipt =
        ProvisioningReceiptV1::new(dispatch.clone(), h.at, MutationReceipt::Task(upid.clone()))
            .unwrap();
    let preflight_json: Value =
        sqlx::query_scalar("SELECT payload FROM rust_controller.journal_events WHERE event_id=$1")
            .bind(preflight.as_uuid())
            .fetch_one(&f.pool)
            .await
            .unwrap();
    let preflight: ProvisioningEvidenceV1 = serde_json::from_value(preflight_json).unwrap();
    let mut facts = preflight.facts().clone();
    facts.binding =
        ProvisioningBindingV1::new(h.run, h.operation, h.attempt, &h.workflow, &facts.plan, 9)
            .unwrap();
    facts.receipt = Some(receipt);
    facts.task = Some(NativeRead::new(
        h.at,
        Ok(TaskStatus::running(upid.clone(), h.at)),
    ));
    let facts = ProvisioningEvidenceV1::new(facts).unwrap();
    let evidence_payload = serde_json::to_value(&facts).unwrap();
    let evidence_hash = event_journal::payload_digest(&evidence_payload).unwrap();
    let park = controller_domain::EventId::new();
    let resumed_at = h.at + chrono::Duration::seconds(2);
    let mut tx = f.pool.begin().await.unwrap();
    let receipt_payload = json!({"contract_version":1,"action":"pve_receipt_captured","dispatch_event_id":dispatch_event,
        "request_sha256":dispatch.request_sha256(),"receipt_kind":"task","upid":upid,"accepted_at":h.at});
    h.event(
        &mut tx,
        receipt_event,
        9,
        "evidence_recorded",
        None,
        receipt_payload,
        h.at,
    )
    .await;
    insert_row(
        &mut tx,
        "osdeploy_pve_receipts",
        &json!({"operation_id":h.operation,"receipt_event_id":receipt_event,
        "receipt_kind":"task","upid":upid,"accepted_at":h.at,"recorded_at":h.at}),
    )
    .await
    .unwrap();
    h.event(
        &mut tx,
        evidence,
        10,
        "evidence_recorded",
        None,
        evidence_payload.clone(),
        h.at,
    )
    .await;
    insert_row(&mut tx, "osdeploy_pve_evidence", &json!({"event_id":evidence,"operation_id":h.operation,"run_id":h.run,
        "attempt_id":h.attempt,"evidence_revision":10,"evidence_sha256":evidence_hash,"source":"fake_pve",
        "evidence_canonical_json":String::from_utf8(event_journal::canonical_json_bytes(&evidence_payload).unwrap()).unwrap()})).await.unwrap();
    h.decision(&mut tx, park, h.envelope("pve_evaluated",10,json!("waiting"),json!({"mode":"outcome","advice":"waiting",
        "evidence_event_id":evidence,"evidence_sha256":evidence_hash,"scope_key":"mutation_clone","deadline_at":h.deadline,
        "reason":"task_running","lease_acquisition_event_id":h.acquisition,
        "schedule":{"mode":"waiting","next_check_at":resumed_at,"unavailable_count":0}}))).await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        12,
        "execution_state_changed",
        Some("waiting"),
        json!({"state":"waiting","decision_event_id":park}),
        h.at,
    )
    .await;
    sqlx::query("DELETE FROM rust_controller.worker_leases WHERE operation_id=$1")
        .bind(h.operation.as_uuid())
        .execute(&mut *tx)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    let parked = f.store.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(parked.state(), controller_domain::ExecutionState::Waiting);
    assert_eq!(parked.next_check_at(), Some(resumed_at));
    sqlx::query("INSERT INTO rust_controller.worker_leases SELECT (jsonb_populate_record(NULL::rust_controller.worker_leases,$1::jsonb)).*").bind(&old_lease).execute(&f.pool).await.unwrap();
    assert!(
        matches!(
            f.store.load_osdeploy_operation(h.operation).await,
            Err(postgres_store::OsDeployExecutionError::Validation)
        ),
        "ordinary park accepted the evaluator lease it ended"
    );

    // A later acquisition is a distinct durable epoch; Waiting itself is not
    // forbidden from carrying that new lease before its resumed start.
    let resume = controller_domain::EventId::new();
    let token = Uuid::now_v7().to_string();
    let mut tx = f.pool.begin().await.unwrap();
    sqlx::query("DELETE FROM rust_controller.worker_leases WHERE operation_id=$1")
        .bind(h.operation.as_uuid())
        .execute(&mut *tx)
        .await
        .unwrap();
    let token_hash: String =
        sqlx::query_scalar("SELECT encode(sha256(convert_to($1,'UTF8')),'hex')")
            .bind(&token)
            .fetch_one(&mut *tx)
            .await
            .unwrap();
    let expires = resumed_at + chrono::Duration::seconds(30);
    let mut payload = h.envelope("lease_acquired",12,Value::Null,json!({"purpose":"resume_evaluation","acquisition_event_id":resume,
        "token_sha256":token_hash,"worker_id":"reload-only-resumed-worker","acquired_at":resumed_at,"expires_at":expires,
        "deadline_at":h.deadline,"prior_schedule_event_id":park}));
    payload["evaluated_at"] = json!(resumed_at);
    h.decision(&mut tx, resume, payload).await;
    insert_row(&mut tx,"osdeploy_lease_epochs",&json!({"acquisition_event_id":resume,"operation_id":h.operation,"run_id":h.run,
        "attempt_id":h.attempt,"executor_kind":"rust","generation":1,"worker_id":"reload-only-resumed-worker",
        "lease_token_sha256":token_hash,"acquired_at":resumed_at,"initial_expires_at":expires,"deadline_at":h.deadline,"purpose":"resume_evaluation"})).await.unwrap();
    let mut resumed_lease = old_lease;
    resumed_lease["worker_id"] = json!("reload-only-resumed-worker");
    resumed_lease["lease_token"] = json!(token);
    resumed_lease["acquired_at"] = json!(resumed_at);
    resumed_lease["heartbeat_at"] = json!(resumed_at);
    resumed_lease["lease_expires_at"] = json!(expires);
    sqlx::query("INSERT INTO rust_controller.worker_leases SELECT (jsonb_populate_record(NULL::rust_controller.worker_leases,$1::jsonb)).*").bind(resumed_lease).execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    let resumed = f.store.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(resumed.state(), controller_domain::ExecutionState::Waiting);
    assert_eq!(resumed.next_check_at(), None);
    assert_eq!(resumed.revision(), 13);
    let evaluation = controller_domain::EventId::new();
    let mut payload = h.envelope(
        "evaluation_started",
        13,
        Value::Null,
        json!({"lease_acquisition_event_id":resume,"activity":"outcome_read"}),
    );
    payload["evaluated_at"] = json!(resumed_at);
    let mut tx = f.pool.begin().await.unwrap();
    h.decision(&mut tx, evaluation, payload).await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        15,
        "execution_state_changed",
        Some("running"),
        json!({"state":"running","decision_event_id":evaluation}),
        resumed_at,
    )
    .await;
    tx.commit().await.unwrap();
    assert_eq!(
        f.store
            .load_osdeploy_operation(h.operation)
            .await
            .unwrap()
            .state(),
        controller_domain::ExecutionState::Running
    );
    let starts:i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='attempt_started'").bind(h.operation.as_uuid()).fetch_one(&f.pool).await.unwrap();
    assert_eq!(
        starts, 1,
        "resuming the same attempt must not fabricate another start"
    );
}

#[tokio::test]
async fn ordinary_lease_reload_preserves_exact_terminal_residual_lease() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    let expiry = controller_domain::EventId::new();
    let mut payload = h.envelope("activated_scope_expired",3,json!("unknown"),json!({"scope_key":"mutation_clone",
        "anchor_operation_id":h.operation,"anchor_event_id":h.activation,"deadline_at":h.deadline,
        "pe_complete_operation_id":null,"pe_complete_decision_event_id":null,"reason":"phase_deadline_expired"}));
    payload["evaluated_at"] = json!(h.deadline);
    let mut tx = f.pool.begin().await.unwrap();
    h.decision(&mut tx, expiry, payload).await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        5,
        "execution_state_changed",
        Some("unknown"),
        json!({"state":"unknown","decision_event_id":expiry}),
        h.deadline,
    )
    .await;
    tx.commit().await.unwrap();
    assert_eq!(
        f.store
            .load_osdeploy_operation(h.operation)
            .await
            .unwrap()
            .state(),
        controller_domain::ExecutionState::Unknown
    );
    let leases: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM rust_controller.worker_leases WHERE operation_id=$1",
    )
    .bind(h.operation.as_uuid())
    .fetch_one(&f.pool)
    .await
    .unwrap();
    assert_eq!(leases, 1);
    sqlx::query("UPDATE rust_controller.worker_leases SET worker_id='wrong-residual-owner' WHERE operation_id=$1").bind(h.operation.as_uuid()).execute(&f.pool).await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn reload_rejects_changed_binding_scope_epoch_and_aggregate_times() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    for (table, change, restore) in [
        (
            "osdeploy_attempt_bindings",
            "activated_at=activated_at+interval '1 second'",
            "activated_at=activated_at-interval '1 second'",
        ),
        (
            "osdeploy_deadlines",
            "opened_at=opened_at+interval '1 second',deadline_at=deadline_at+interval '1 second'",
            "opened_at=opened_at-interval '1 second',deadline_at=deadline_at-interval '1 second'",
        ),
        (
            "osdeploy_lease_epochs",
            "initial_expires_at=initial_expires_at+interval '1 second'",
            "initial_expires_at=initial_expires_at-interval '1 second'",
        ),
        ("osdeploy_lease_epochs", "generation=2", "generation=1"),
    ] {
        assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
        f.corrupt_immutable(
            table,
            &format!("UPDATE rust_controller.{table} SET {change}"),
        )
        .await;
        assert!(
            matches!(
                f.store.load_osdeploy_operation(h.operation).await,
                Err(postgres_store::OsDeployExecutionError::Validation)
            ),
            "{table}: {change}"
        );
        f.corrupt_immutable(
            table,
            &format!("UPDATE rust_controller.{table} SET {restore}"),
        )
        .await;
    }
    sqlx::query("UPDATE rust_controller.operations SET revision=revision+1 WHERE operation_id=$1")
        .bind(h.operation.as_uuid())
        .execute(&f.pool)
        .await
        .unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn reload_rejects_acquisition_without_its_atomic_state_event() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
    let mut tx = f.pool.begin().await.unwrap();
    sqlx::query(
        "DELETE FROM rust_controller.journal_events WHERE operation_id=$1 AND aggregate_revision=3",
    )
    .bind(h.operation.as_uuid())
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query(
        "UPDATE rust_controller.operations SET revision=2,state='pending' WHERE operation_id=$1",
    )
    .bind(h.operation.as_uuid())
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query("UPDATE rust_controller.attempts SET state='pending' WHERE attempt_id=$1")
        .bind(h.attempt.as_uuid())
        .execute(&mut *tx)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn evidence_restore_rejects_source_fence_hash_shape_missing_nulls_and_duplicates() {
    let f = Fixture::new().await;
    let source = reload_source();
    let h = ReloadHistory::with_plan(
        &f,
        osdeploy_support::altered(|v| {
            v["template_config_sha256"] = json!(source.template_fingerprint().unwrap())
        }),
    )
    .await;
    let (_, event, _) = h.dispatched(&f, source).await;
    let original: Value =
        sqlx::query_scalar("SELECT payload FROM rust_controller.journal_events WHERE event_id=$1")
            .bind(event.as_uuid())
            .fetch_one(&f.pool)
            .await
            .unwrap();
    assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
    let mut cases = Vec::new();
    let mut v = original.clone();
    v["binding"]["evidence_fence"] = json!(7);
    cases.push(("changed fence", v));
    let mut v = original.clone();
    v["binding"]["run_id"] = json!(controller_domain::RunId::new());
    cases.push(("foreign run", v));
    let mut v = original.clone();
    v["binding"]["workflow_sha256"] = json!(h.workflow.to_uppercase());
    cases.push(("uppercase hash", v));
    let mut v = original.clone();
    v["source"] = json!("pve_api");
    cases.push(("unadmitted source", v));
    let mut v = original.clone();
    v.as_object_mut().unwrap().remove("receipt");
    cases.push(("missing required nullable", v));
    let mut v = original.clone();
    v["unexpected"] = json!(false);
    cases.push(("unknown root", v));
    for (label, v) in cases {
        replace_reload_evidence(&f, event, &v, None).await;
        assert!(
            matches!(
                f.store.load_osdeploy_operation(h.operation).await,
                Err(postgres_store::OsDeployExecutionError::Validation)
            ),
            "{label}"
        );
        replace_reload_evidence(&f, event, &original, None).await;
        assert!(
            f.store.load_osdeploy_operation(h.operation).await.is_ok(),
            "restored {label}"
        );
    }
    let duplicate = String::from_utf8(event_journal::canonical_json_bytes(&original).unwrap())
        .unwrap()
        .replacen("\"source\":", "\"source\":\"fake_pve\",\"source\":", 1);
    replace_reload_evidence(&f, event, &original, Some(&duplicate)).await;
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

async fn replace_reload_evidence(
    f: &Fixture,
    event: controller_domain::EventId,
    payload: &Value,
    text: Option<&str>,
) {
    let canonical =
        String::from_utf8(event_journal::canonical_json_bytes(payload).unwrap()).unwrap();
    let hash = event_journal::payload_digest(payload).unwrap();
    let mut tx = f.pool.begin().await.unwrap();
    sqlx::raw_sql(
        "ALTER TABLE rust_controller.osdeploy_pve_evidence DISABLE TRIGGER osdeploy_no_mutation",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query("UPDATE rust_controller.osdeploy_pve_evidence SET evidence_canonical_json=$2,evidence_sha256=$3 WHERE event_id=$1")
        .bind(event.as_uuid()).bind(text.unwrap_or(&canonical)).bind(&hash).execute(&mut *tx).await.unwrap();
    sqlx::query(
        "UPDATE rust_controller.journal_events SET payload=$2,payload_digest=$3 WHERE event_id=$1",
    )
    .bind(event.as_uuid())
    .bind(payload)
    .bind(hash)
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::raw_sql(
        "ALTER TABLE rust_controller.osdeploy_pve_evidence ENABLE TRIGGER osdeploy_no_mutation",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    tx.commit().await.unwrap();
}

#[tokio::test]
async fn receipt_reload_keeps_original_evidence_fence_and_rejects_foreign_dispatch() {
    let f = Fixture::new().await;
    let source = reload_source();
    let p = osdeploy_support::altered(|v| {
        v["template_config_sha256"] = json!(source.template_fingerprint().unwrap())
    });
    let h = ReloadHistory::with_plan(&f, p).await;
    let (dispatch, evidence, original) = h.dispatched(&f, source).await;
    let s = f.store.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(s.dispatch(), Some(&original));
    assert_eq!(
        s.dispatch().unwrap().request().binding().evidence_fence(),
        6
    );
    let receipt = controller_domain::EventId::new();
    let at = h.at + chrono::Duration::seconds(1);
    let upid = "UPID:node-a:00000001:00000001:00000001:qmclone:900:root@pam:";
    let payload = json!({"contract_version":1,"action":"pve_receipt_captured","dispatch_event_id":dispatch,
        "request_sha256":original.request_sha256(),"receipt_kind":"task","upid":upid,"accepted_at":at});
    let mut tx = f.pool.begin().await.unwrap();
    h.event(
        &mut tx,
        receipt,
        9,
        "evidence_recorded",
        None,
        payload.clone(),
        at,
    )
    .await;
    insert_row(
        &mut tx,
        "osdeploy_pve_receipts",
        &json!({"operation_id":h.operation,"receipt_event_id":receipt,
        "receipt_kind":"task","upid":upid,"accepted_at":at,"recorded_at":at}),
    )
    .await
    .unwrap();
    tx.commit().await.unwrap();
    let s = f.other.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(s.revision(), 9);
    assert_eq!(s.state(), controller_domain::ExecutionState::Running);
    assert_eq!(s.receipt().unwrap().dispatch(), &original);
    assert_eq!(s.receipt().unwrap().accepted_at(), at);
    assert_eq!(
        s.dispatch().unwrap().request().binding().evidence_fence(),
        6
    );
    // A plausible generic event is never promoted to the immutable typed index.
    f.corrupt_immutable("osdeploy_pve_evidence",&format!("UPDATE rust_controller.osdeploy_pve_evidence SET evidence_revision=10 WHERE event_id='{}'",evidence.as_uuid())).await;
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
    f.corrupt_immutable("osdeploy_pve_evidence",&format!("UPDATE rust_controller.osdeploy_pve_evidence SET evidence_revision=7 WHERE event_id='{}'",evidence.as_uuid())).await;
    let mut foreign = payload;
    foreign["dispatch_event_id"] = json!(controller_domain::EventId::new());
    sqlx::query(
        "UPDATE rust_controller.journal_events SET payload=$2,payload_digest=$3 WHERE event_id=$1",
    )
    .bind(receipt.as_uuid())
    .bind(&foreign)
    .bind(event_journal::payload_digest(&foreign).unwrap())
    .execute(&f.pool)
    .await
    .unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn reload_rejects_dispatch_at_the_original_lease_expiry() {
    let f = Fixture::new().await;
    let source = reload_source();
    let h = ReloadHistory::with_plan(
        &f,
        osdeploy_support::altered(|v| {
            v["template_config_sha256"] = json!(source.template_fingerprint().unwrap())
        }),
    )
    .await;
    let (event, _, _) = h.dispatched(&f, source).await;
    assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
    let mut payload: Value =
        sqlx::query_scalar("SELECT payload FROM rust_controller.journal_events WHERE event_id=$1")
            .bind(event.as_uuid())
            .fetch_one(&f.pool)
            .await
            .unwrap();
    let at = h.at + chrono::Duration::seconds(30);
    payload["evaluated_at"] = json!(at);
    payload["detail"]["dispatched_at"] = json!(at);
    let mut tx = f.pool.begin().await.unwrap();
    sqlx::raw_sql("ALTER TABLE rust_controller.osdeploy_pve_dispatches DISABLE TRIGGER osdeploy_no_mutation; ALTER TABLE rust_controller.osdeploy_decisions DISABLE TRIGGER osdeploy_no_mutation").execute(&mut *tx).await.unwrap();
    sqlx::query(
        "UPDATE rust_controller.osdeploy_pve_dispatches SET dispatched_at=$2 WHERE operation_id=$1",
    )
    .bind(h.operation.as_uuid())
    .bind(at)
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query("UPDATE rust_controller.osdeploy_decisions SET evaluated_at=$2,payload_canonical_json=$3 WHERE event_id=$1")
        .bind(event.as_uuid()).bind(at).bind(String::from_utf8(event_journal::canonical_json_bytes(&payload).unwrap()).unwrap()).execute(&mut *tx).await.unwrap();
    sqlx::query("UPDATE rust_controller.journal_events SET observed_at=$2,payload=$3,payload_digest=$4 WHERE event_id=$1")
        .bind(event.as_uuid()).bind(at).bind(&payload).bind(event_journal::payload_digest(&payload).unwrap()).execute(&mut *tx).await.unwrap();
    sqlx::raw_sql("SET CONSTRAINTS ALL IMMEDIATE; ALTER TABLE rust_controller.osdeploy_pve_dispatches ENABLE TRIGGER osdeploy_no_mutation; ALTER TABLE rust_controller.osdeploy_decisions ENABLE TRIGGER osdeploy_no_mutation").execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn selected_terminal_decision_survives_later_cancellation_control() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    let expiry = controller_domain::EventId::new();
    let cancellation = controller_domain::EventId::new();
    let mut tx = f.pool.begin().await.unwrap();
    let mut payload=h.envelope("activated_scope_expired",3,json!("unknown"),json!({"scope_key":"mutation_clone",
        "anchor_operation_id":h.operation,"anchor_event_id":h.activation,"deadline_at":h.deadline,
        "pe_complete_operation_id":null,"pe_complete_decision_event_id":null,"reason":"phase_deadline_expired"}));
    payload["evaluated_at"] = json!(h.deadline);
    h.decision(&mut tx, expiry, payload).await;
    h.event(
        &mut tx,
        controller_domain::EventId::new(),
        5,
        "execution_state_changed",
        Some("unknown"),
        json!({"state":"unknown","decision_event_id":expiry}),
        h.deadline,
    )
    .await;
    sqlx::query("DELETE FROM rust_controller.worker_leases WHERE operation_id=$1")
        .bind(h.operation.as_uuid())
        .execute(&mut *tx)
        .await
        .unwrap();
    let mut payload = h.envelope(
        "run_cancelled",
        5,
        Value::Null,
        json!({"reason":"run_cancellation_requested"}),
    );
    payload["evaluated_at"] = json!(h.deadline);
    h.decision(&mut tx, cancellation, payload).await;
    insert_row(
        &mut tx,
        "osdeploy_run_cancellations",
        &json!({"run_id":h.run,"anchor_operation_id":h.operation,
        "decision_event_id":cancellation,"generation":1,"requested_at":h.deadline}),
    )
    .await
    .unwrap();
    tx.commit().await.unwrap();
    let s = f.store.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(s.revision(), 6);
    assert!(s.cancelled());
    assert_eq!(s.state(), controller_domain::ExecutionState::Unknown);
    // Remove the selected decision index while keeping its journal fact;
    // the remaining control event must not stand in for the terminal proof.
    let mut tx = f.pool.begin().await.unwrap();
    sqlx::raw_sql(
        "ALTER TABLE rust_controller.osdeploy_decisions DISABLE TRIGGER osdeploy_no_mutation",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query("DELETE FROM rust_controller.osdeploy_decisions WHERE event_id=$1")
        .bind(expiry.as_uuid())
        .execute(&mut *tx)
        .await
        .unwrap();
    // Drain this test-owned deletion's deferred FK checks before restoring its
    // immutable trigger; ALTER with pending constraint triggers is forbidden.
    sqlx::raw_sql("SET CONSTRAINTS ALL IMMEDIATE; ALTER TABLE rust_controller.osdeploy_decisions ENABLE TRIGGER osdeploy_no_mutation").execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn declaration_and_bound_activation_reload_are_observations_without_writes() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    let before = f.snapshot().await;
    let s = f.store.load_osdeploy_operation(h.operation).await.unwrap();
    assert_eq!(s.operation_id(), h.operation);
    assert_eq!(s.run_id(), h.run);
    assert_eq!(s.revision(), 3);
    assert_eq!(s.state(), controller_domain::ExecutionState::Leased);
    assert_eq!(s.attempt_id(), Some(h.attempt));
    assert_eq!(s.activated_at(), Some(h.at));
    assert_eq!(s.deadline_at(), Some(h.deadline));
    assert!(
        s.dispatch().is_none()
            && s.receipt().is_none()
            && s.next_check_at().is_none()
            && !s.cancelled()
    );
    let reg = f.store.load_osdeploy_registration(h.run).await.unwrap();
    for stage in osdeploy_adapter::OsDeployStage::ALL.into_iter().skip(1) {
        let s = f
            .other
            .load_osdeploy_operation(reg.ids().operation(stage))
            .await
            .unwrap();
        assert_eq!(s.state(), controller_domain::ExecutionState::Pending);
        assert_eq!(s.revision(), 0);
        assert!(s.attempt_id().is_none());
    }
    assert_eq!(before, f.snapshot().await);
}

#[tokio::test]
async fn reload_rejects_extra_attempt_and_changed_original_attempt_times() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
    for statement in [
        "UPDATE rust_controller.attempts SET started_at=started_at+interval '1 second'",
        "UPDATE rust_controller.attempts SET deadline_at=deadline_at+interval '1 second'",
        "UPDATE rust_controller.attempts SET attempt_number=2",
    ] {
        let mut tx = f.pool.begin().await.unwrap();
        sqlx::raw_sql(statement).execute(&mut *tx).await.unwrap();
        // Use the private transaction through its production public read by
        // committing corruption in this isolated fixture, then restore exactly.
        tx.commit().await.unwrap();
        assert!(
            matches!(
                f.store.load_osdeploy_operation(h.operation).await,
                Err(postgres_store::OsDeployExecutionError::Validation)
            ),
            "{statement}"
        );
        sqlx::query(
            "UPDATE rust_controller.attempts SET started_at=$1,deadline_at=$2,attempt_number=1",
        )
        .bind(h.at)
        .bind(h.deadline)
        .execute(&f.pool)
        .await
        .unwrap();
    }
    sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state) VALUES($1,$2,2,'pending')")
        .bind(controller_domain::AttemptId::new().as_uuid()).bind(h.operation.as_uuid()).execute(&f.pool).await.unwrap();
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn reload_rejects_wrong_decision_kind_revision_hash_and_missing_index() {
    let f = Fixture::new().await;
    let h = ReloadHistory::new(&f).await;
    assert!(f.store.load_osdeploy_operation(h.operation).await.is_ok());
    for (field, value) in [
        ("event_kind", "'evidence_recorded'"),
        ("payload_digest", "repeat('a',64)"),
        ("observed_at", "observed_at+interval '1 second'"),
    ] {
        let original: Value = sqlx::query_scalar(
            "SELECT to_jsonb(e) FROM rust_controller.journal_events e WHERE event_id=$1",
        )
        .bind(h.activation.as_uuid())
        .fetch_one(&f.pool)
        .await
        .unwrap();
        sqlx::raw_sql(&format!(
            "UPDATE rust_controller.journal_events SET {field}={value} WHERE event_id='{}'",
            h.activation.as_uuid()
        ))
        .execute(&f.pool)
        .await
        .unwrap();
        assert!(
            matches!(
                f.store.load_osdeploy_operation(h.operation).await,
                Err(postgres_store::OsDeployExecutionError::Validation)
            ),
            "{field}"
        );
        let replacement = original[field].as_str().unwrap();
        sqlx::raw_sql(&format!(
            "UPDATE rust_controller.journal_events SET {field}='{replacement}' WHERE event_id='{}'",
            h.activation.as_uuid()
        ))
        .execute(&f.pool)
        .await
        .unwrap();
    }
    f.corrupt_immutable(
        "osdeploy_decisions",
        &format!(
            "UPDATE rust_controller.osdeploy_decisions SET decision_revision=9 WHERE event_id='{}'",
            h.activation.as_uuid()
        ),
    )
    .await;
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
    f.corrupt_immutable(
        "osdeploy_decisions",
        &format!(
            "UPDATE rust_controller.osdeploy_decisions SET decision_revision=1 WHERE event_id='{}'",
            h.activation.as_uuid()
        ),
    )
    .await;
    f.corrupt_immutable("osdeploy_decisions",&format!("UPDATE rust_controller.osdeploy_decisions SET stage_sha256=repeat('a',64) WHERE event_id='{}'",h.activation.as_uuid())).await;
    assert!(matches!(
        f.store.load_osdeploy_operation(h.operation).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    ));
}

#[tokio::test]
async fn migration_is_additive_and_registration_stays_declaration_only() {
    let f = Fixture::new().await;
    let count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_tables WHERE schemaname='rust_controller' AND tablename IN \
        ('osdeploy_decisions','osdeploy_deadlines','osdeploy_attempt_bindings','osdeploy_lease_epochs',\
        'osdeploy_pve_evidence','osdeploy_pve_dispatches','osdeploy_pve_receipts',\
        'osdeploy_run_cancellations','osdeploy_schedule_projection')",
    )
    .fetch_one(&f.pool)
    .await
    .unwrap();
    assert_eq!(count, 9);
    f.store
        .enqueue_osdeploy(controller_domain::RunId::new(), &plan())
        .await
        .unwrap();
    let before = f.snapshot().await;
    f.store.migrate().await.unwrap();
    assert_eq!(f.snapshot().await, before);
    f.assert_fresh_counts().await;
    for name in TABLES {
        let count: i64 =
            sqlx::query_scalar(&format!("SELECT count(*) FROM rust_controller.{name}"))
                .fetch_one(&f.pool)
                .await
                .unwrap();
        assert_eq!(count, 0, "{name}");
    }
}

// Deliberately SQL-local constraint inputs, never executable predecessor proofs.
// The real execution loader must reject these empty canonical payloads.
struct SchemaRows(Vec<(&'static str, Value)>);

impl SchemaRows {
    async fn new(f: &Fixture) -> Self {
        let run = controller_domain::RunId::new();
        f.store.enqueue_osdeploy(run, &plan()).await.unwrap();
        let operations: Vec<Uuid> = sqlx::query_scalar(
            "SELECT operation_id FROM rust_controller.osdeploy_operation_plans ORDER BY ordinal LIMIT 2",
        ).fetch_all(&f.pool).await.unwrap();
        let mut rows = Vec::new();
        for (index, operation) in operations.into_iter().enumerate() {
            let attempt = Uuid::now_v7();
            let events: Vec<Uuid> = (0..8).map(|_| Uuid::now_v7()).collect();
            let scope = ["mutation_clone", "mutation_disk_capacity"][index];
            sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state,started_at,deadline_at) VALUES($1,$2,1,'leased','2026-09-05T12:00:00Z','2026-09-05T12:01:00Z')")
                .bind(attempt).bind(operation).execute(&f.pool).await.unwrap();
            for (revision, event) in events.iter().enumerate() {
                sqlx::query("INSERT INTO rust_controller.journal_events(event_id,operation_id,attempt_id,aggregate_revision,semantic_key,payload_digest,event_kind,payload,observed_at) VALUES($1,$2,$3,$4,$5,$6,$7,'{}','2026-09-05T12:00:00Z')")
                    .bind(event).bind(operation).bind(attempt).bind((revision + 1) as i64)
                    .bind(format!("schema-only-{revision}")).bind(osdeploy_support::SHA)
                    .bind(if [2,4].contains(&revision) { "evidence_recorded" } else { "decision_recorded" })
                    .execute(&f.pool).await.unwrap();
            }
            for (revision, action, resolution) in [
                (1, "stage_activated", None),
                (2, "lease_acquired", None),
                (4, "pve_dispatch_committed", Some("ready")),
                (6, "pve_evaluated", Some("waiting")),
                (7, "run_cancelled", None),
            ] {
                rows.push(("osdeploy_decisions", json!({
                    "operation_id":operation,"decision_revision":revision,"event_id":events[revision-1],
                    "run_id":run.as_uuid(),"attempt_id":attempt,"action":action,"resolution":resolution,
                    "workflow_sha256":osdeploy_support::SHA,"stage_sha256":osdeploy_support::SHA,
                    "generation":1,"evaluated_at":"2026-09-05T12:00:00Z","payload_canonical_json":"{}"
                })));
            }
            rows.push(("osdeploy_deadlines",json!({"run_id":run.as_uuid(),"scope_key":scope,
                "anchor_operation_id":operation,"anchor_event_id":events[0],"opened_at":"2026-09-05T12:00:00Z",
                "budget_seconds":60,"deadline_at":"2026-09-05T12:01:00Z"})));
            rows.push(("osdeploy_attempt_bindings",json!({"operation_id":operation,"run_id":run.as_uuid(),
                "attempt_id":attempt,"scope_key":scope,"activation_event_id":events[0],
                "activated_at":"2026-09-05T12:00:00Z","deadline_at":"2026-09-05T12:01:00Z","activation_mode":"leased"})));
            rows.push(("osdeploy_lease_epochs",json!({"acquisition_event_id":events[1],"operation_id":operation,
                "run_id":run.as_uuid(),"attempt_id":attempt,"executor_kind":"rust","generation":1,"worker_id":"schema-test",
                "lease_token_sha256":if index == 0 { "a".repeat(64) } else { "b".repeat(64) },
                "acquired_at":"2026-09-05T12:00:00Z","initial_expires_at":"2026-09-05T12:00:30Z",
                "deadline_at":"2026-09-05T12:01:00Z","purpose":"initial_evaluation"})));
            rows.push(("osdeploy_pve_evidence",json!({"event_id":events[2],"operation_id":operation,
                "run_id":run.as_uuid(),"attempt_id":attempt,"evidence_revision":3,"evidence_sha256":osdeploy_support::SHA,
                "source":"fake_pve","evidence_canonical_json":"{}"})));
            rows.push(("osdeploy_pve_dispatches",json!({"operation_id":operation,"run_id":run.as_uuid(),
                "attempt_id":attempt,"dispatch_event_id":events[3],"dispatch_revision":4,"preflight_event_id":events[2],
                "workflow_sha256":osdeploy_support::SHA,"pve_plan_sha256":osdeploy_support::SHA,
                "request_sha256":osdeploy_support::SHA,"request_canonical_json":"{}","source":"fake_pve",
                "original_generation":1,"dispatched_at":"2026-09-05T12:00:00Z","lease_acquisition_event_id":events[1]})));
            rows.push(("osdeploy_pve_receipts",json!({"operation_id":operation,"receipt_event_id":events[4],
                "receipt_kind":"task","upid":"schema-only-upid","accepted_at":"2026-09-05T12:00:00Z","recorded_at":"2026-09-05T12:00:00Z"})));
            if index == 0 {
                rows.push(("osdeploy_run_cancellations",json!({"run_id":run.as_uuid(),"anchor_operation_id":operation,
                    "decision_event_id":events[6],"generation":1,"requested_at":"2026-09-05T12:00:00Z"})));
            }
            rows.push(("osdeploy_schedule_projection",json!({"operation_id":operation,"run_id":run.as_uuid(),
                "attempt_id":attempt,"mode":"waiting","basis_event_id":events[5],"basis_revision":6,"scope_key":scope,
                "next_check_at":"2026-09-05T12:00:02Z","unavailable_count":0,"rebuilt_through_revision":7})));
        }
        // All immediate typed targets precede references, including cross-operation test inputs.
        rows.sort_by_key(|(table, _)| TABLES.iter().position(|name| name == table).unwrap());
        Self(rows)
    }

    fn row(&self, table: &str) -> &Value {
        &self.0.iter().find(|(name, _)| *name == table).unwrap().1
    }

    async fn insert(&self, connection: &mut PgConnection) -> Result<(), sqlx::Error> {
        for (table, row) in &self.0 {
            insert_row(connection, table, row).await?;
        }
        sqlx::raw_sql("SET CONSTRAINTS ALL IMMEDIATE")
            .execute(connection)
            .await?;
        Ok(())
    }

    async fn reject(&self, f: &Fixture, table: &str, field: &str, value: Value, code: &str) {
        let mut changed = Self(self.0.clone());
        changed
            .0
            .iter_mut()
            .find(|(name, _)| *name == table)
            .unwrap()
            .1[field] = value;
        let mut tx = f.pool.begin().await.unwrap();
        let error = changed
            .insert(&mut tx)
            .await
            .expect_err(&format!("accepted {table}.{field}"));
        assert_sqlstate(&error, code, &format!("{table}.{field}"));
        tx.rollback().await.unwrap();
    }
}

async fn insert_row(
    connection: &mut PgConnection,
    table: &str,
    row: &Value,
) -> Result<(), sqlx::Error> {
    assert!(TABLES.contains(&table), "unowned schema input");
    sqlx::query(&format!("INSERT INTO rust_controller.{table} SELECT (jsonb_populate_record(NULL::rust_controller.{table},$1::jsonb)).*"))
        .bind(row).execute(connection).await?;
    Ok(())
}

fn assert_sqlstate(error: &sqlx::Error, expected: &str, context: &str) {
    assert_eq!(
        error.as_database_error().and_then(|e| e.code()).as_deref(),
        Some(expected),
        "{context}: {error}"
    );
}

#[tokio::test]
async fn immutable_tables_reject_update_delete_and_truncate_and_repair_each_trigger() {
    let f = Fixture::new().await;
    let rows = SchemaRows::new(&f).await;
    let mut tx = f.pool.begin().await.unwrap();
    rows.insert(&mut tx).await.unwrap();
    tx.commit().await.unwrap();
    let before = f.snapshot().await;
    for table in &TABLES[..8] {
        let column = if *table == "osdeploy_deadlines" || *table == "osdeploy_run_cancellations" {
            "run_id"
        } else {
            "operation_id"
        };
        for statement in [
            format!("UPDATE rust_controller.{table} SET {column}={column}"),
            format!("DELETE FROM rust_controller.{table}"),
            format!("TRUNCATE rust_controller.{table} CASCADE"),
        ] {
            let error = sqlx::raw_sql(&statement)
                .execute(&f.pool)
                .await
                .unwrap_err();
            assert_sqlstate(&error, "23514", &statement);
            assert_eq!(
                error.as_database_error().unwrap().message(),
                "native durable record is immutable"
            );
        }
        for trigger in ["osdeploy_no_mutation", "osdeploy_no_truncate"] {
            sqlx::raw_sql(&format!(
                "DROP TRIGGER {trigger} ON rust_controller.{table}"
            ))
            .execute(&f.pool)
            .await
            .unwrap();
            f.store.migrate().await.unwrap();
            let triggers: i64 = sqlx::query_scalar("SELECT count(*) FROM pg_trigger WHERE tgrelid=$1::regclass AND tgname IN ('osdeploy_no_mutation','osdeploy_no_truncate') AND NOT tgisinternal")
                .bind(format!("rust_controller.{table}")).fetch_one(&f.pool).await.unwrap();
            assert_eq!(triggers, 2, "{table}/{trigger}");
            let statement = if trigger == "osdeploy_no_mutation" {
                format!("UPDATE rust_controller.{table} SET {column}={column}")
            } else {
                format!("TRUNCATE rust_controller.{table} CASCADE")
            };
            let error = sqlx::raw_sql(&statement)
                .execute(&f.pool)
                .await
                .unwrap_err();
            assert_sqlstate(&error, "23514", &statement);
            assert_eq!(
                error.as_database_error().unwrap().message(),
                "native durable record is immutable"
            );
            assert_eq!(f.snapshot().await, before);
        }
    }
    // Scheduling is the single disposable mutable table.
    sqlx::query("UPDATE rust_controller.osdeploy_schedule_projection SET unavailable_count=1")
        .execute(&f.pool)
        .await
        .unwrap();
    sqlx::query("DELETE FROM rust_controller.osdeploy_schedule_projection")
        .execute(&f.pool)
        .await
        .unwrap();
    sqlx::raw_sql("TRUNCATE rust_controller.osdeploy_schedule_projection")
        .execute(&f.pool)
        .await
        .unwrap();
}

#[tokio::test]
async fn schema_rejects_foreign_operation_references_and_duplicate_bindings_and_dispatches() {
    let f = Fixture::new().await;
    let rows = SchemaRows::new(&f).await;
    for (table, field, source, source_field) in [
        (
            "osdeploy_decisions",
            "event_id",
            "osdeploy_decisions",
            "event_id",
        ),
        (
            "osdeploy_decisions",
            "attempt_id",
            "osdeploy_attempt_bindings",
            "attempt_id",
        ),
        (
            "osdeploy_deadlines",
            "anchor_event_id",
            "osdeploy_deadlines",
            "anchor_event_id",
        ),
        (
            "osdeploy_attempt_bindings",
            "activation_event_id",
            "osdeploy_attempt_bindings",
            "activation_event_id",
        ),
        (
            "osdeploy_lease_epochs",
            "attempt_id",
            "osdeploy_attempt_bindings",
            "attempt_id",
        ),
        (
            "osdeploy_pve_evidence",
            "attempt_id",
            "osdeploy_attempt_bindings",
            "attempt_id",
        ),
        (
            "osdeploy_pve_dispatches",
            "dispatch_event_id",
            "osdeploy_pve_dispatches",
            "dispatch_event_id",
        ),
        (
            "osdeploy_pve_dispatches",
            "preflight_event_id",
            "osdeploy_pve_evidence",
            "event_id",
        ),
        (
            "osdeploy_pve_dispatches",
            "lease_acquisition_event_id",
            "osdeploy_lease_epochs",
            "acquisition_event_id",
        ),
        (
            "osdeploy_pve_receipts",
            "receipt_event_id",
            "osdeploy_pve_receipts",
            "receipt_event_id",
        ),
        (
            "osdeploy_run_cancellations",
            "decision_event_id",
            "osdeploy_decisions",
            "event_id",
        ),
        (
            "osdeploy_schedule_projection",
            "basis_event_id",
            "osdeploy_schedule_projection",
            "basis_event_id",
        ),
    ] {
        let other_operation = rows
            .0
            .iter()
            .rev()
            .find(|(name, _)| *name == "osdeploy_attempt_bindings")
            .unwrap()
            .1["operation_id"]
            .clone();
        let other = rows
            .0
            .iter()
            .find(|(name, row)| {
                *name == source
                    && (row["operation_id"] == other_operation
                        || row["anchor_operation_id"] == other_operation)
            })
            .unwrap()
            .1[source_field]
            .clone();
        rows.reject(&f, table, field, other, "23503").await;
    }
    for table in ["osdeploy_attempt_bindings", "osdeploy_pve_dispatches"] {
        let mut tx = f.pool.begin().await.unwrap();
        rows.insert(&mut tx).await.unwrap();
        let error = insert_row(&mut tx, table, rows.row(table))
            .await
            .unwrap_err();
        assert_sqlstate(&error, "23505", table);
        tx.rollback().await.unwrap();
    }
    let generic_event: Uuid = sqlx::query_scalar("SELECT event_id FROM rust_controller.journal_events WHERE operation_id=$1 AND aggregate_revision=8")
        .bind(Uuid::parse_str(rows.row("osdeploy_pve_dispatches")["operation_id"].as_str().unwrap()).unwrap())
        .fetch_one(&f.pool).await.unwrap();
    for field in ["preflight_event_id", "lease_acquisition_event_id"] {
        // A same-operation generic journal event cannot replace typed evidence or an epoch.
        rows.reject(
            &f,
            "osdeploy_pve_dispatches",
            field,
            json!(generic_event),
            "23503",
        )
        .await;
    }
    for table in TABLES
        .into_iter()
        .filter(|table| *table != "osdeploy_pve_receipts")
    {
        rows.reject(&f, table, "run_id", json!(Uuid::now_v7()), "23503")
            .await;
    }
}

#[tokio::test]
async fn schema_closes_actions_resolutions_and_attempt_nullability() {
    let f = Fixture::new().await;
    let rows = SchemaRows::new(&f).await;
    for (action, resolution, nullable) in [
        ("stage_activated", None, false),
        ("lease_acquired", None, false),
        ("evaluation_started", None, false),
        ("lease_renewed", None, false),
        ("pve_dispatch_committed", Some("ready"), false),
        ("pve_evaluated", Some("waiting"), false),
        ("lease_reclaimed_same_attempt", None, false),
        ("evaluation_reparked", Some("waiting"), false),
        ("scope_expired_before_activation", Some("unknown"), true),
        ("activated_scope_expired", Some("unknown"), false),
        ("run_cancelled", None, true),
        ("stage_cancelled_unexposed", Some("blocked"), true),
        ("stage_cancelled_exposed", Some("unknown"), false),
        ("residual_lease_revoked", None, false),
        ("reconciliation_scheduled", None, false),
        ("lease_expired_uncertain", Some("unknown"), false),
    ] {
        for attempt_null in [false, true] {
            for candidate_resolution in [
                None,
                Some("ready"),
                Some("waiting"),
                Some("satisfied"),
                Some("failed"),
                Some("blocked"),
                Some("unknown"),
                Some("conflicted"),
            ] {
                let mut row = rows.row("osdeploy_decisions").clone();
                row["action"] = json!(action);
                row["resolution"] = json!(candidate_resolution);
                if attempt_null {
                    row["attempt_id"] = Value::Null;
                }
                let resolution_valid = if action == "pve_evaluated" {
                    candidate_resolution.is_some_and(|v| v != "ready")
                } else {
                    candidate_resolution == resolution
                };
                let attempt_valid = if action == "scope_expired_before_activation" {
                    attempt_null
                } else {
                    !attempt_null || nullable
                };
                let mut tx = f.pool.begin().await.unwrap();
                let result = insert_row(&mut tx, "osdeploy_decisions", &row).await;
                if resolution_valid && attempt_valid {
                    result.unwrap();
                } else {
                    assert_sqlstate(&result.unwrap_err(), "23514", action);
                }
                tx.rollback().await.unwrap();
            }
        }
    }
    for action in ["grace_wait_activated", "pve_receipt_captured", "arbitrary"] {
        rows.reject(&f, "osdeploy_decisions", "action", json!(action), "23514")
            .await;
    }
}

#[tokio::test]
async fn schema_rejects_invalid_scope_hash_counter_time_and_payload_rows() {
    let f = Fixture::new().await;
    let rows = SchemaRows::new(&f).await;
    for (table, field, value) in [
        ("osdeploy_decisions", "decision_revision", json!(0)),
        ("osdeploy_decisions", "generation", json!(0)),
        (
            "osdeploy_decisions",
            "workflow_sha256",
            json!("A".repeat(64)),
        ),
        ("osdeploy_decisions", "stage_sha256", json!("bad")),
        ("osdeploy_decisions", "resolution", json!("pending")),
        ("osdeploy_deadlines", "scope_key", json!("arbitrary")),
        ("osdeploy_deadlines", "budget_seconds", json!(0)),
        ("osdeploy_deadlines", "budget_seconds", json!(86401)),
        (
            "osdeploy_deadlines",
            "deadline_at",
            json!("2026-09-05T12:01:01Z"),
        ),
        (
            "osdeploy_attempt_bindings",
            "activation_mode",
            json!("resumed"),
        ),
        (
            "osdeploy_attempt_bindings",
            "deadline_at",
            json!("2026-09-05T12:00:00Z"),
        ),
        ("osdeploy_lease_epochs", "generation", json!(0)),
        ("osdeploy_lease_epochs", "executor_kind", json!("python")),
        ("osdeploy_lease_epochs", "worker_id", json!("   ")),
        (
            "osdeploy_lease_epochs",
            "lease_token_sha256",
            json!("raw-token"),
        ),
        ("osdeploy_lease_epochs", "purpose", json!("send")),
        (
            "osdeploy_lease_epochs",
            "initial_expires_at",
            json!("2026-09-05T12:00:00Z"),
        ),
        (
            "osdeploy_lease_epochs",
            "initial_expires_at",
            json!("2026-09-05T12:01:01Z"),
        ),
        ("osdeploy_pve_evidence", "evidence_revision", json!(0)),
        ("osdeploy_pve_evidence", "source", json!("real_pve")),
        ("osdeploy_pve_evidence", "evidence_sha256", json!("bad")),
        ("osdeploy_pve_dispatches", "dispatch_revision", json!(0)),
        ("osdeploy_pve_dispatches", "original_generation", json!(0)),
        ("osdeploy_pve_dispatches", "source", json!("real_pve")),
        ("osdeploy_pve_dispatches", "workflow_sha256", json!("bad")),
        ("osdeploy_pve_dispatches", "pve_plan_sha256", json!("bad")),
        ("osdeploy_pve_dispatches", "request_sha256", json!("bad")),
        ("osdeploy_pve_receipts", "receipt_kind", json!("other")),
        ("osdeploy_pve_receipts", "upid", Value::Null),
        ("osdeploy_pve_receipts", "upid", json!("  ")),
        (
            "osdeploy_pve_receipts",
            "receipt_kind",
            json!("synchronous"),
        ),
        (
            "osdeploy_pve_receipts",
            "recorded_at",
            json!("2026-09-05T11:59:59Z"),
        ),
        ("osdeploy_run_cancellations", "generation", json!(0)),
        ("osdeploy_schedule_projection", "mode", json!("execute")),
        ("osdeploy_schedule_projection", "basis_revision", json!(0)),
        (
            "osdeploy_schedule_projection",
            "rebuilt_through_revision",
            json!(0),
        ),
        (
            "osdeploy_schedule_projection",
            "unavailable_count",
            json!(-1),
        ),
        (
            "osdeploy_schedule_projection",
            "unavailable_count",
            json!(5),
        ),
        ("osdeploy_schedule_projection", "next_check_at", Value::Null),
    ] {
        rows.reject(&f, table, field, value, "23514").await;
    }
    for (table, field, limit) in [
        ("osdeploy_decisions", "payload_canonical_json", 65536),
        ("osdeploy_pve_evidence", "evidence_canonical_json", 1048576),
        ("osdeploy_pve_dispatches", "request_canonical_json", 1048576),
    ] {
        for value in [
            json!("[]"),
            json!("null"),
            json!(format!("{{\"x\":\"{}\"}}", "é".repeat(limit / 2))),
        ] {
            rows.reject(&f, table, field, value, "23514").await;
        }
        rows.reject(&f, table, field, json!("{invalid"), "22P02")
            .await;
        // Exactly the byte limit is admitted; non-ASCII oversize above tests octets, not characters.
        let mut exact = SchemaRows(rows.0.clone());
        exact
            .0
            .iter_mut()
            .find(|(name, _)| *name == table)
            .unwrap()
            .1[field] = json!(format!("{{\"x\":\"{}\"}}", "x".repeat(limit - 8)));
        let mut tx = f.pool.begin().await.unwrap();
        exact.insert(&mut tx).await.unwrap();
        tx.rollback().await.unwrap();
    }
}

#[tokio::test]
async fn typed_decision_constraints_are_deferred_but_journal_targets_are_immediate() {
    let f = Fixture::new().await;
    let rows = SchemaRows::new(&f).await;
    for (table, constraint, event_field) in [
        (
            "osdeploy_deadlines",
            "osdeploy_deadline_anchor_decision_fk",
            "anchor_event_id",
        ),
        (
            "osdeploy_attempt_bindings",
            "osdeploy_binding_activation_decision_fk",
            "activation_event_id",
        ),
        (
            "osdeploy_lease_epochs",
            "osdeploy_epoch_acquisition_decision_fk",
            "acquisition_event_id",
        ),
        (
            "osdeploy_pve_dispatches",
            "osdeploy_dispatch_decision_fk",
            "dispatch_event_id",
        ),
        (
            "osdeploy_run_cancellations",
            "osdeploy_cancellation_decision_fk",
            "decision_event_id",
        ),
        (
            "osdeploy_schedule_projection",
            "osdeploy_schedule_basis_decision_fk",
            "basis_event_id",
        ),
    ] {
        let flags: (bool,bool) = sqlx::query_as("SELECT condeferrable,condeferred FROM pg_constraint WHERE connamespace='rust_controller'::regnamespace AND conname=$1")
            .bind(constraint).fetch_one(&f.pool).await.unwrap();
        assert_eq!(flags, (true, true), "{constraint}");
        let mut tx = f.pool.begin().await.unwrap();
        let target = rows.row(table)[event_field].clone();
        let decision = rows
            .0
            .iter()
            .find(|(name, row)| *name == "osdeploy_decisions" && row["event_id"] == target)
            .unwrap();
        for (name, row) in &rows.0 {
            if *name == "osdeploy_decisions" && row["event_id"] == target {
                continue;
            }
            insert_row(&mut tx, name, row).await.unwrap();
        }
        let error = sqlx::raw_sql(&format!(
            "SET CONSTRAINTS rust_controller.{constraint} IMMEDIATE"
        ))
        .execute(&mut *tx)
        .await
        .unwrap_err();
        assert_sqlstate(&error, "23503", constraint);
        tx.rollback().await.unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        for (name, row) in &rows.0 {
            if *name == "osdeploy_decisions" && row["event_id"] == target {
                continue;
            }
            insert_row(&mut tx, name, row).await.unwrap();
        }
        insert_row(&mut tx, decision.0, &decision.1).await.unwrap();
        sqlx::raw_sql("SET CONSTRAINTS ALL IMMEDIATE")
            .execute(&mut *tx)
            .await
            .unwrap();
        tx.rollback().await.unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        for (name, row) in &rows.0 {
            if *name == table {
                let mut missing_event = row.clone();
                missing_event[event_field] = json!(Uuid::now_v7());
                let error = insert_row(&mut tx, name, &missing_event).await.unwrap_err();
                assert_sqlstate(&error, "23503", "journal target must exist at insertion");
                break;
            }
            insert_row(&mut tx, name, row).await.unwrap();
        }
        tx.rollback().await.unwrap();
    }
}

async fn assert_clean_before_durability(f: &Fixture, pid: i32, obstruction: bool) {
    let mut connection = tokio::time::timeout(Duration::from_secs(5), f.pool.acquire())
        .await
        .expect("primary reacquisition timed out")
        .unwrap();
    let (actual_pid, unassigned, answer): (i32, bool, i32) =
        sqlx::query_as("SELECT pg_backend_pid(),txid_current_if_assigned() IS NULL,40+2")
            .fetch_one(&mut *connection)
            .await
            .unwrap();
    assert_eq!(actual_pid, pid, "migration replaced the primary backend");
    assert!(unassigned, "untracked transaction retained");
    assert_eq!(answer, 42);
    let tables: Vec<String> = sqlx::query_scalar("SELECT tablename FROM pg_tables WHERE schemaname='rust_controller' AND tablename=ANY($1) ORDER BY tablename")
        .bind(TABLES.as_slice()).fetch_all(&mut *connection).await.unwrap();
    assert_eq!(
        tables,
        if obstruction {
            vec!["osdeploy_pve_dispatches"]
        } else {
            vec![]
        }
    );
    let authority: (String, i64) = sqlx::query_as(
        "SELECT executor_kind,generation FROM rust_controller.orchestration_authority",
    )
    .fetch_one(&mut *connection)
    .await
    .unwrap();
    assert_eq!(authority, ("rust".into(), 1));
    let runs: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.osdeploy_runs")
        .fetch_one(&mut *connection)
        .await
        .unwrap();
    assert_eq!(runs, 0);
}

#[tokio::test]
async fn migration_failure_rolls_back_and_reuses_clean_connection() {
    let f = Fixture::new_before_durability().await;
    let pid: i32 = sqlx::query_scalar("SELECT pg_backend_pid()")
        .fetch_one(&f.pool)
        .await
        .unwrap();
    sqlx::raw_sql("CREATE TABLE rust_controller.osdeploy_pve_dispatches(broken integer)")
        .execute(&f.pool)
        .await
        .unwrap();
    let error = tokio::time::timeout(Duration::from_secs(5), f.store.migrate())
        .await
        .unwrap()
        .unwrap_err();
    assert!(
        error.to_string().contains("operation_id"),
        "unexpected migration failure: {error}"
    );
    assert_clean_before_durability(&f, pid, true).await;
    sqlx::raw_sql("DROP TABLE rust_controller.osdeploy_pve_dispatches")
        .execute(&f.pool)
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(5), f.store.migrate())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(f.snapshot().await["osdeploy_decisions"], json!([]));
}

#[tokio::test]
async fn cancelled_migration_rolls_back_and_reuses_clean_connection() {
    let f = Fixture::new_before_durability().await;
    let pid: i32 = sqlx::query_scalar("SELECT pg_backend_pid()")
        .fetch_one(&f.pool)
        .await
        .unwrap();
    let observer = tokio::time::timeout(
        Duration::from_secs(5),
        PgPoolOptions::new()
            .max_connections(1)
            .acquire_timeout(Duration::from_secs(1))
            .connect_with((*f.pool.connect_options()).clone()),
    )
    .await
    .expect("observer connection timed out")
    .unwrap();
    let mut barrier = observer.acquire().await.unwrap();
    let nonce = Uuid::now_v7();
    let key = i64::from_be_bytes(nonce.as_bytes()[8..16].try_into().unwrap());
    let name = format!("durability_barrier_{}", nonce.simple());
    tokio::time::timeout(
        Duration::from_secs(5),
        sqlx::query("SELECT pg_advisory_lock($1)")
            .bind(key)
            .execute(&mut *barrier),
    )
    .await
    .expect("owned barrier acquisition timed out")
    .unwrap();
    sqlx::raw_sql(&format!("CREATE FUNCTION rust_controller.{name}() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN IF EXISTS (SELECT 1 FROM pg_event_trigger_ddl_commands() WHERE command_tag='CREATE TABLE' AND object_identity='rust_controller.osdeploy_decisions') THEN PERFORM pg_advisory_xact_lock({key}); END IF; END $$; CREATE EVENT TRIGGER {name} ON ddl_command_end WHEN TAG IN ('CREATE TABLE') EXECUTE FUNCTION rust_controller.{name}();"))
        .execute(&f.pool).await.unwrap();
    let mut migration = Box::pin(f.store.migrate());
    tokio::select! {
        result = &mut migration => panic!("migration passed its barrier: {result:?}"),
        reached = tokio::time::timeout(Duration::from_secs(5),async {
            loop {
                let waiting: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pg_locks l JOIN pg_stat_activity a USING(pid) WHERE l.pid=$1 AND l.locktype='advisory' AND NOT l.granted AND l.classid=$2::bigint::oid AND l.objid=$3::bigint::oid AND l.objsubid=1 AND a.wait_event='advisory' AND a.query LIKE '%CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_decisions%')")
                    .bind(pid).bind(((key as u64)>>32) as i64).bind(((key as u64)&0xffff_ffff) as i64)
                    .fetch_one(&mut *barrier).await.unwrap();
                if waiting { break; }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }) => reached.expect("exact 0005 advisory barrier not reached")
    }
    let waiting: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_locks WHERE pid=$1 AND locktype='advisory' AND NOT granted",
    )
    .bind(pid)
    .fetch_one(&mut *barrier)
    .await
    .unwrap();
    assert_eq!(waiting, 1, "0005 barrier not reached");
    drop(migration);
    // Release before pool acquisition so SQLx's queued rollback can finish.
    let unlocked: bool = tokio::time::timeout(
        Duration::from_secs(5),
        sqlx::query_scalar("SELECT pg_advisory_unlock($1)")
            .bind(key)
            .fetch_one(&mut *barrier),
    )
    .await
    .expect("owned barrier release timed out")
    .unwrap();
    assert!(unlocked);
    assert_clean_before_durability(&f, pid, false).await;
    sqlx::raw_sql(&format!(
        "DROP EVENT TRIGGER {name}; DROP FUNCTION rust_controller.{name}();"
    ))
    .execute(&f.pool)
    .await
    .unwrap();
    tokio::time::timeout(Duration::from_secs(5), f.store.migrate())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(f.snapshot().await["osdeploy_decisions"], json!([]));
    drop(barrier);
    observer.close().await;
}
#[tokio::test]
async fn real_no_growth_baseline_enables_configure_without_capacity_dispatch() {
    let s = osdeploy_execution_support::Scenario::new(300, false).await;
    for stage in [
        osdeploy_adapter::OsDeployStage::Clone,
        osdeploy_adapter::OsDeployStage::DiskCapacity,
        osdeploy_adapter::OsDeployStage::ConfigurePe,
    ] {
        assert_eq!(
            s.finish_stage(stage).await,
            controller_domain::ExecutionState::Satisfied
        );
    }
    let capacity =
        s.db.store
            .load_osdeploy_operation(
                s.ids
                    .operation(osdeploy_adapter::OsDeployStage::DiskCapacity),
            )
            .await
            .unwrap();
    assert!(capacity.dispatch().is_none());
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 2);
    assert!(matches!(
        s.db.scheduler()
            .claim_osdeploy_bound(
                s.ids.operation(osdeploy_adapter::OsDeployStage::StartPe),
                s.ids.workflow_sha256(),
                1
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::CapabilityUnavailable)
    ));
}
#[tokio::test]
async fn task6_waiting_due_resume_preserves_original_attempt_and_replays_decision() {
    use osdeploy_adapter::OsDeployStage;
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let r = s.ready(OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Waiting
    );
    let parked =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    let before = s.db.snapshot().await;
    assert!(
        scheduler
            .resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                parked.revision(),
                s.ids.workflow_sha256(),
                1
            )
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(s.db.snapshot().await, before);
    tokio::time::timeout(
        std::time::Duration::from_secs(4),
        sqlx::query(
            "SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))",
        )
        .bind(parked.next_check_at().unwrap())
        .execute(&s.db.pool),
    )
    .await
    .unwrap()
    .unwrap();
    let other = s.db.other_scheduler();
    let (a, b) = tokio::time::timeout(std::time::Duration::from_secs(10), async {
        tokio::join!(
            scheduler.resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                parked.revision(),
                s.ids.workflow_sha256(),
                1
            ),
            other.resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                parked.revision(),
                s.ids.workflow_sha256(),
                1
            )
        )
    })
    .await
    .unwrap();
    let grants: Vec<_> = [a, b]
        .into_iter()
        .filter_map(|x| match x {
            Ok(grant) => grant,
            Err(postgres_store::OsDeployExecutionError::FenceLost) => None,
            Err(error) => panic!("unexpected resume race error: {error:?}"),
        })
        .collect();
    assert_eq!(grants.len(), 1);
    let resumed = &grants[0];
    assert_eq!(resumed.attempt_id(), r.grant.attempt_id());
    assert_eq!(resumed.attempt_number(), 1);
    assert_eq!(resumed.deadline_at(), r.grant.deadline_at());
    assert_ne!(resumed.lease_token(), r.grant.lease_token());
    let winner = if resumed.worker_id() == r.grant.worker_id() {
        &scheduler
    } else {
        &other
    };
    winner
        .start_osdeploy_bound(resumed, s.ids.workflow_sha256())
        .await
        .unwrap();
    winner
        .start_osdeploy_bound(resumed, s.ids.workflow_sha256())
        .await
        .unwrap();
    assert!(
        scheduler
            .start_osdeploy_bound(&r.grant, s.ids.workflow_sha256())
            .await
            .is_err()
    );
    let MutationReceipt::Task(upid) = receipt else {
        panic!("Clone must capture its actual task")
    };
    s.fake.complete_provisioning_task(&upid).unwrap();
    // Use winner's owner token while retaining the original dispatch/receipt.
    let snap =
        s.db.store
            .load_osdeploy_operation(resumed.operation_id())
            .await
            .unwrap();
    let c =
        s.db.store
            .load_osdeploy_pve_context(
                resumed.operation_id(),
                snap.revision(),
                ProvisioningEvaluationModeV1::Outcome,
            )
            .await
            .unwrap();
    let e = s.collect(&c).await;
    let event =
        s.db.store
            .record_osdeploy_pve_evidence(
                resumed.operation_id(),
                resumed.attempt_id(),
                snap.revision(),
                &e,
            )
            .await
            .unwrap();
    let revision =
        s.db.store
            .load_osdeploy_operation(resumed.operation_id())
            .await
            .unwrap()
            .revision();
    assert_eq!(
        winner
            .decide_osdeploy_pve(resumed, revision, event)
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Satisfied)
    );
    let final_snapshot =
        s.db.store
            .load_osdeploy_operation(resumed.operation_id())
            .await
            .unwrap();
    assert_eq!(final_snapshot.activated_at(), parked.activated_at());
    assert_eq!(final_snapshot.dispatch(), parked.dispatch());
    assert_eq!(final_snapshot.receipt(), parked.receipt());
    assert!(final_snapshot.next_check_at().is_none());
    let before = s.db.snapshot().await;
    assert_eq!(
        winner
            .decide_osdeploy_pve(resumed, revision, event)
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Satisfied)
    );
    assert_eq!(s.db.snapshot().await, before);
    let counts: (i64,i64,i64) = sqlx::query_as("SELECT (SELECT count(*) FROM rust_controller.attempts),(SELECT count(*) FROM rust_controller.journal_events WHERE event_kind='attempt_started'),(SELECT count(*) FROM rust_controller.osdeploy_lease_epochs)").fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(counts, (1, 1, 2));
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}
#[tokio::test]
async fn task6_growth_history_survives_old_deadline_and_all_later_stages_stay_closed() {
    use osdeploy_adapter::OsDeployStage;
    let s = osdeploy_execution_support::Scenario::new(5, true).await;
    assert_eq!(
        s.finish_stage(OsDeployStage::Clone).await,
        controller_domain::ExecutionState::Satisfied
    );
    let clone =
        s.db.store
            .load_osdeploy_operation(s.ids.operation(OsDeployStage::Clone))
            .await
            .unwrap();
    let selected: (uuid::Uuid,chrono::DateTime<chrono::Utc>) = sqlx::query_as("SELECT event_id,evaluated_at FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND resolution='satisfied'").bind(clone.operation_id().as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert!(selected.1 < clone.deadline_at().unwrap());
    tokio::time::timeout(
        std::time::Duration::from_secs(7),
        sqlx::query(
            "SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))",
        )
        .bind(clone.deadline_at().unwrap())
        .execute(&s.db.pool),
    )
    .await
    .unwrap()
    .unwrap();
    for stage in [OsDeployStage::DiskCapacity, OsDeployStage::ConfigurePe] {
        assert_eq!(
            s.finish_stage(stage).await,
            controller_domain::ExecutionState::Satisfied
        );
    }
    for stage in [
        OsDeployStage::Clone,
        OsDeployStage::DiskCapacity,
        OsDeployStage::ConfigurePe,
    ] {
        let snapshot =
            s.db.other
                .load_osdeploy_operation(s.ids.operation(stage))
                .await
                .unwrap();
        assert_eq!(
            snapshot.state(),
            controller_domain::ExecutionState::Satisfied
        );
        assert!(snapshot.dispatch().is_some());
        assert!(snapshot.receipt().is_some());
    }
    let unchanged: (uuid::Uuid,chrono::DateTime<chrono::Utc>) = sqlx::query_as("SELECT event_id,evaluated_at FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND resolution='satisfied'").bind(clone.operation_id().as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(unchanged, selected);
    let config =
        s.db.store
            .load_osdeploy_operation(s.ids.operation(OsDeployStage::ConfigurePe))
            .await
            .unwrap();
    assert!(matches!(
        config.receipt().unwrap().receipt(),
        pve_port::MutationReceipt::SynchronousAccepted
    ));
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 3);
    // Possessing a complete typed observation is not stage admission. The
    // evidence ingress gate must reject StartPe before treating caller-supplied
    // facts as either an activation witness or a selected outcome.
    let context =
        s.db.store
            .load_osdeploy_pve_context(
                config.operation_id(),
                config.revision(),
                pve_port::ProvisioningEvaluationModeV1::Outcome,
            )
            .await
            .unwrap();
    let observation = s.collect(&context).await;
    let start =
        s.db.other
            .load_osdeploy_operation(s.ids.operation(OsDeployStage::StartPe))
            .await
            .unwrap();
    let ingress_before = s.db.snapshot().await;
    #[cfg(feature = "fixture-ipc")]
    {
        assert!(matches!(
            s.db.store
                .probe_osdeploy_start_pe_fixture(start.operation_id(), start.revision(), None)
                .await,
            Err(postgres_store::OsDeployExecutionError::CapabilityUnavailable)
        ));
        assert!(matches!(
            s.db.store
                .probe_osdeploy_start_pe_fixture(start.operation_id(), start.revision() + 1, None)
                .await,
            Err(postgres_store::OsDeployExecutionError::FenceLost)
        ));
        assert!(matches!(
            s.db.store
                .probe_osdeploy_start_pe_fixture(config.operation_id(), config.revision(), None)
                .await,
            Err(postgres_store::OsDeployExecutionError::Validation)
        ));
        assert_eq!(s.db.snapshot().await, ingress_before);
    }
    assert!(matches!(
        s.db.store
            .load_osdeploy_pve_context(
                start.operation_id(),
                start.revision(),
                pve_port::ProvisioningEvaluationModeV1::Outcome
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::CapabilityUnavailable)
    ));
    assert!(matches!(
        s.db.store
            .record_osdeploy_pve_evidence(
                start.operation_id(),
                config.attempt_id().unwrap(),
                start.revision(),
                &observation
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::CapabilityUnavailable)
    ));
    assert_eq!(s.db.snapshot().await, ingress_before);
    let start_after =
        s.db.other
            .load_osdeploy_operation(start.operation_id())
            .await
            .unwrap();
    assert_eq!(
        start_after.state(),
        controller_domain::ExecutionState::Pending
    );
    assert!(start_after.attempt_id().is_none());
    assert!(start_after.dispatch().is_none());
    assert!(start_after.receipt().is_none());
    let before = s.db.snapshot().await;
    for stage in OsDeployStage::ALL.into_iter().skip(3) {
        assert!(matches!(
            s.db.scheduler()
                .claim_osdeploy_bound(s.ids.operation(stage), s.ids.workflow_sha256(), 1)
                .await,
            Err(postgres_store::OsDeployExecutionError::CapabilityUnavailable)
        ));
        assert!(matches!(
            s.db.scheduler()
                .resume_osdeploy_bound(
                    s.ids.operation(stage),
                    controller_domain::AttemptId::new(),
                    0,
                    s.ids.workflow_sha256(),
                    1
                )
                .await,
            Err(postgres_store::OsDeployExecutionError::CapabilityUnavailable)
        ));
    }
    assert_eq!(s.db.snapshot().await, before);
}

#[tokio::test]
async fn task6_original_scope_deadline_rejects_late_apparent_success() {
    use osdeploy_adapter::OsDeployStage;
    let s = osdeploy_execution_support::Scenario::new(2, true).await;
    let r = s.ready(OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let (event, revision) = s.observation(&r.grant).await;
    tokio::time::timeout(
        std::time::Duration::from_secs(4),
        sqlx::query(
            "SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))",
        )
        .bind(r.grant.deadline_at())
        .execute(&s.db.pool),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(
        scheduler
            .decide_osdeploy_pve(&r.grant, revision, event)
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Unknown)
    );
    let row: (chrono::DateTime<chrono::Utc>,String) = sqlx::query_as("SELECT evaluated_at,payload_canonical_json::jsonb->'detail'->>'reason' FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND action='activated_scope_expired'").bind(r.grant.operation_id().as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert!(row.0 >= *r.grant.deadline_at());
    assert_eq!(row.1, "phase_deadline_expired");
    let snapshot =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert_eq!(snapshot.deadline_at(), Some(*r.grant.deadline_at()));
    assert!(snapshot.next_check_at().is_none());
    assert!(
        scheduler
            .claim_osdeploy_bound(
                s.ids.operation(OsDeployStage::DiskCapacity),
                s.ids.workflow_sha256(),
                1
            )
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}

#[tokio::test]
async fn task6_unavailable_preflight_is_unknown_without_dispatch_or_recovery() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let g = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    s.fake.enqueue_provisioning_config_read(
        NodeName::parse("node-a").unwrap(),
        Vmid::new(900).unwrap(),
        FakeProvisioningConfigReadV1::Error(PveReadError::TimedOut),
    );
    let (event, revision) = s.observation(&g).await;
    let scheduler = s.db.scheduler();
    assert_eq!(
        scheduler
            .decide_osdeploy_pve(&g, revision, event)
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Unknown)
    );
    let snapshot =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    assert!(snapshot.dispatch().is_none());
    assert!(snapshot.receipt().is_none());
    assert!(snapshot.next_check_at().is_none());
    let before = s.db.snapshot().await;
    assert_eq!(
        scheduler
            .decide_osdeploy_pve(&g, revision, event)
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Unknown)
    );
    assert!(matches!(
        scheduler.decide_osdeploy_pve(&g, revision + 1, event).await,
        Err(postgres_store::OsDeployExecutionError::Conflict)
    ));
    assert!(
        scheduler
            .claim_osdeploy_bound(g.operation_id(), s.ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        scheduler
            .resume_osdeploy_bound(
                g.operation_id(),
                g.attempt_id(),
                snapshot.revision(),
                s.ids.workflow_sha256(),
                1
            )
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(s.db.snapshot().await, before);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
}

#[tokio::test]
async fn task6_accepted_task_failure_is_selected_failed() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
            FakeMutationOutcome::AcceptedTaskFails,
        )
        .unwrap();
    assert_eq!(
        s.finish_stage(osdeploy_adapter::OsDeployStage::Clone).await,
        controller_domain::ExecutionState::Failed
    );
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.worker_leases")
        .fetch_one(&s.db.pool)
        .await
        .unwrap();
    assert_eq!(count, 0);
}

#[tokio::test]
async fn task6_synchronous_configure_response_loss_keeps_original_dispatch_unknown() {
    use osdeploy_adapter::OsDeployStage;
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, false).await;
    for stage in [OsDeployStage::Clone, OsDeployStage::DiskCapacity] {
        assert_eq!(
            s.finish_stage(stage).await,
            controller_domain::ExecutionState::Satisfied
        );
    }
    let r = s.ready(OsDeployStage::ConfigurePe).await;
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::ConfigurePe, None),
            FakeMutationOutcome::AppliedResponseLost,
        )
        .unwrap();
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    assert_eq!(
        permit.submit_fake_once(s.fake.as_ref()).await,
        Err(PveWriteError::OutcomeUnknown)
    );
    drop(capture);
    let recorded = s.fake.recorded_provisioning_submissions();
    assert_eq!(recorded.len(), 2);
    assert!(matches!(
        recorded[1].acceptance().unwrap().receipt(),
        MutationReceipt::SynchronousAccepted
    ));
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Unknown
    );
    let snapshot =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert!(snapshot.dispatch().is_some());
    assert!(snapshot.receipt().is_none());
    assert!(
        scheduler
            .begin_osdeploy_pve_dispatch(&r.grant, snapshot.revision(), r.event, &r.request)
            .await
            .is_err()
    );
    assert!(
        scheduler
            .claim_osdeploy_bound(r.grant.operation_id(), s.ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 2);
}
#[tokio::test]
async fn task6_decision_and_park_rollback_each_write_and_deferred_commit() {
    use postgres_store::OsDeployExecutionError as E;
    use pve_port::*;
    for (waiting, expired) in [(false, false), (true, false), (false, true)] {
        let s =
            osdeploy_execution_support::Scenario::new(if expired { 2 } else { 300 }, true).await;
        if waiting {
            s.fake
                .enqueue_provisioning_outcome(
                    ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
                    FakeMutationOutcome::AcceptedTaskDelayed,
                )
                .unwrap();
        }
        let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
        let scheduler = s.db.scheduler();
        let (permit, capture) = scheduler
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
        let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
        scheduler
            .record_osdeploy_pve_receipt(&capture, &receipt)
            .await
            .unwrap();
        let (event, revision) = s.observation(&r.grant).await;
        if expired {
            tokio::time::timeout(std::time::Duration::from_secs(4),sqlx::query("SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))").bind(r.grant.deadline_at()).execute(&s.db.pool)).await.unwrap().unwrap();
        }
        sqlx::raw_sql("CREATE SEQUENCE decision_write_number; CREATE TABLE decision_fault(target bigint); INSERT INTO decision_fault VALUES(0); CREATE FUNCTION decision_fail_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF nextval('decision_write_number')=(SELECT target FROM decision_fault) THEN RAISE EXCEPTION 'owned_decision_fault'; END IF; IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW; END $$;").execute(&s.db.pool).await.unwrap();
        let tables = [
            "journal_events",
            "outbox",
            "osdeploy_decisions",
            "operations",
            "operation_projection",
            "attempts",
            "osdeploy_schedule_projection",
            "worker_leases",
        ];
        for table in tables {
            sqlx::query(&format!("CREATE TRIGGER decision_write_fault BEFORE INSERT OR UPDATE OR DELETE ON rust_controller.{table} FOR EACH ROW EXECUTE FUNCTION decision_fail_write()")).execute(&s.db.pool).await.unwrap();
        }
        let before = s.db.snapshot().await;
        let boundaries = if waiting { 14 } else { 13 };
        for boundary in 1..=boundaries {
            sqlx::query("UPDATE decision_fault SET target=$1")
                .bind(boundary)
                .execute(&s.db.pool)
                .await
                .unwrap();
            sqlx::query("SELECT setval('decision_write_number',1,false)")
                .execute(&s.db.pool)
                .await
                .unwrap();
            assert_eq!(
                scheduler
                    .decide_osdeploy_pve(&r.grant, revision, event)
                    .await,
                Err(E::StorageUnavailable),
                "waiting={waiting} boundary={boundary}"
            );
            assert_eq!(
                s.db.snapshot().await,
                before,
                "waiting={waiting} boundary={boundary}"
            );
        }
        for table in tables {
            sqlx::query(&format!(
                "DROP TRIGGER decision_write_fault ON rust_controller.{table}"
            ))
            .execute(&s.db.pool)
            .await
            .unwrap();
        }
        sqlx::raw_sql("CREATE FUNCTION decision_fail_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned_decision_commit_fault'; END $$; CREATE CONSTRAINT TRIGGER decision_commit_fault AFTER INSERT ON rust_controller.osdeploy_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION decision_fail_commit();").execute(&s.db.pool).await.unwrap();
        assert_eq!(
            scheduler
                .decide_osdeploy_pve(&r.grant, revision, event)
                .await,
            Err(E::StorageUnavailable)
        );
        assert_eq!(s.db.snapshot().await, before);
        sqlx::query("DROP TRIGGER decision_commit_fault ON rust_controller.osdeploy_decisions")
            .execute(&s.db.pool)
            .await
            .unwrap();
        let expected = if waiting {
            postgres_store::OsDeployProgress::Waiting
        } else {
            postgres_store::OsDeployProgress::Decided(if expired {
                controller_domain::ExecutionState::Unknown
            } else {
                controller_domain::ExecutionState::Satisfied
            })
        };
        assert_eq!(
            scheduler
                .decide_osdeploy_pve(&r.grant, revision, event)
                .await
                .unwrap(),
            expected
        );
        let before = s.db.snapshot().await;
        assert_eq!(
            scheduler
                .decide_osdeploy_pve(&r.grant, revision, event)
                .await
                .unwrap(),
            expected
        );
        assert_eq!(s.db.snapshot().await, before);
        let (leases,schedules): (i64,i64) = sqlx::query_as("SELECT (SELECT count(*) FROM rust_controller.worker_leases),(SELECT count(*) FROM rust_controller.osdeploy_schedule_projection)").fetch_one(&s.db.pool).await.unwrap();
        assert_eq!((leases, schedules), (0, i64::from(waiting)));
    }
}

#[tokio::test]
async fn task6_resume_rollback_each_write_and_deferred_commit() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Waiting
    );
    let parked =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    tokio::time::timeout(
        std::time::Duration::from_secs(4),
        sqlx::query(
            "SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))",
        )
        .bind(parked.next_check_at())
        .execute(&s.db.pool),
    )
    .await
    .unwrap()
    .unwrap();
    sqlx::raw_sql("CREATE SEQUENCE resume_write_number; CREATE TABLE resume_fault(target bigint); INSERT INTO resume_fault VALUES(0); CREATE FUNCTION resume_fail_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF nextval('resume_write_number')=(SELECT target FROM resume_fault) THEN RAISE EXCEPTION 'owned_resume_fault'; END IF; IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW; END $$;").execute(&s.db.pool).await.unwrap();
    let tables = [
        "journal_events",
        "outbox",
        "osdeploy_decisions",
        "operations",
        "operation_projection",
        "osdeploy_lease_epochs",
        "worker_leases",
        "osdeploy_schedule_projection",
    ];
    for table in tables {
        sqlx::query(&format!("CREATE TRIGGER resume_write_fault BEFORE INSERT OR UPDATE OR DELETE ON rust_controller.{table} FOR EACH ROW EXECUTE FUNCTION resume_fail_write()")).execute(&s.db.pool).await.unwrap();
    }
    let before = s.db.snapshot().await;
    for boundary in 1..=9 {
        sqlx::query("UPDATE resume_fault SET target=$1")
            .bind(boundary)
            .execute(&s.db.pool)
            .await
            .unwrap();
        sqlx::query("SELECT setval('resume_write_number',1,false)")
            .execute(&s.db.pool)
            .await
            .unwrap();
        assert!(
            matches!(
                scheduler
                    .resume_osdeploy_bound(
                        r.grant.operation_id(),
                        r.grant.attempt_id(),
                        parked.revision(),
                        s.ids.workflow_sha256(),
                        1
                    )
                    .await,
                Err(postgres_store::OsDeployExecutionError::StorageUnavailable)
            ),
            "boundary={boundary}"
        );
        assert_eq!(s.db.snapshot().await, before, "boundary={boundary}");
    }
    for table in tables {
        sqlx::query(&format!(
            "DROP TRIGGER resume_write_fault ON rust_controller.{table}"
        ))
        .execute(&s.db.pool)
        .await
        .unwrap();
    }
    sqlx::raw_sql("CREATE FUNCTION resume_fail_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned_resume_commit_fault'; END $$; CREATE CONSTRAINT TRIGGER resume_commit_fault AFTER INSERT ON rust_controller.osdeploy_lease_epochs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION resume_fail_commit();").execute(&s.db.pool).await.unwrap();
    assert!(matches!(
        scheduler
            .resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                parked.revision(),
                s.ids.workflow_sha256(),
                1
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::StorageUnavailable)
    ));
    assert_eq!(s.db.snapshot().await, before);
    sqlx::query("DROP TRIGGER resume_commit_fault ON rust_controller.osdeploy_lease_epochs")
        .execute(&s.db.pool)
        .await
        .unwrap();
    let g = scheduler
        .resume_osdeploy_bound(
            r.grant.operation_id(),
            r.grant.attempt_id(),
            parked.revision(),
            s.ids.workflow_sha256(),
            1,
        )
        .await
        .unwrap()
        .unwrap();
    assert_eq!(g.attempt_id(), r.grant.attempt_id());
}
#[tokio::test]
async fn task6_config_and_identity_changes_select_conflicted() {
    use pve_port::*;
    for (field, value) in [
        ("cores", serde_json::json!(7)),
        (
            "uuid",
            serde_json::json!("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ),
    ] {
        let s = osdeploy_execution_support::Scenario::new(300, true).await;
        let stage = if field == "uuid" {
            assert_eq!(
                s.finish_stage(osdeploy_adapter::OsDeployStage::Clone).await,
                controller_domain::ExecutionState::Satisfied
            );
            osdeploy_adapter::OsDeployStage::DiskCapacity
        } else {
            osdeploy_adapter::OsDeployStage::Clone
        };
        let r = s.ready(stage).await;
        let scheduler = s.db.scheduler();
        let (permit, capture) = scheduler
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
        let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
        scheduler
            .record_osdeploy_pve_receipt(&capture, &receipt)
            .await
            .unwrap();
        let vm = r.request.plan().expected().vm();
        let config = s
            .fake
            .provisioning_vm_config(vm.node(), vm.target_vmid())
            .await
            .unwrap();
        let mut changed = serde_json::to_value(config).unwrap();
        changed[field] = value;
        s.fake
            .replace_provisioning_vm(
                serde_json::from_value(changed).unwrap(),
                PowerState::Stopped,
            )
            .unwrap();
        assert_eq!(
            s.observe_and_decide(&r.grant).await,
            controller_domain::ExecutionState::Conflicted,
            "field={field}"
        );
        let next = if field == "uuid" {
            osdeploy_adapter::OsDeployStage::ConfigurePe
        } else {
            osdeploy_adapter::OsDeployStage::DiskCapacity
        };
        assert!(
            scheduler
                .claim_osdeploy_bound(s.ids.operation(next), s.ids.workflow_sha256(), 1)
                .await
                .unwrap()
                .is_none()
        );
        assert_eq!(
            s.fake.recorded_provisioning_submissions().len(),
            if field == "uuid" { 2 } else { 1 }
        );
    }
}

#[tokio::test]
async fn task6_current_cas_serializes_preflight_selection_against_dispatch() {
    use postgres_store::{OsDeployExecutionError as E, OsDeployProgress as P};
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    s.fake.enqueue_provisioning_config_read(
        NodeName::parse("node-a").unwrap(),
        Vmid::new(900).unwrap(),
        FakeProvisioningConfigReadV1::Error(PveReadError::TimedOut),
    );
    let (event, revision) = s.observation(&r.grant).await;
    let other = postgres_store::Scheduler::new(
        s.db.other.clone(),
        postgres_store::ExecutorKind::Rust,
        1,
        "osdeploy-worker",
    )
    .unwrap();
    let (decision, dispatch) = tokio::time::timeout(std::time::Duration::from_secs(10), async {
        tokio::join!(
            scheduler.decide_osdeploy_pve(&r.grant, revision, event),
            other.begin_osdeploy_pve_dispatch(&r.grant, revision, r.event, &r.request)
        )
    })
    .await
    .unwrap();
    match (decision, dispatch) {
        (Ok(P::Decided(controller_domain::ExecutionState::Unknown)), Err(E::FenceLost)) => {
            assert!(s.fake.recorded_provisioning_submissions().is_empty())
        }
        (Err(E::FenceLost), Ok((permit, capture))) => {
            let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
            scheduler
                .record_osdeploy_pve_receipt(&capture, &receipt)
                .await
                .unwrap();
            assert_eq!(
                s.observe_and_decide(&r.grant).await,
                controller_domain::ExecutionState::Satisfied
            );
            assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
        }
        (decision, dispatch) => panic!(
            "unexpected decision/dispatch race: {decision:?}, dispatch_ok={}",
            dispatch.is_ok()
        ),
    }
    let selected: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.osdeploy_decisions WHERE resolution IS NOT NULL AND resolution<>'ready'").fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(selected, 1);
}

#[tokio::test]
async fn task6_paused_original_send_may_capture_after_unknown_without_resend() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let pause = s
        .fake
        .pause_provisioning_submission(ProvisioningFaultSelectorV1::new(
            ProvisioningActionV1::Clone,
            Some(r.grant.operation_id()),
        ));
    let send = permit.submit_fake_once(s.fake.as_ref());
    tokio::pin!(send);
    tokio::time::timeout(std::time::Duration::from_secs(3),async {
        tokio::select! { _=pause.entered()=>{}, result=&mut send=>panic!("original send completed before pause: {result:?}") }
    }).await.unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Unknown
    );
    pause.release();
    let receipt = tokio::time::timeout(std::time::Duration::from_secs(3), send)
        .await
        .unwrap()
        .unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let snapshot =
        s.db.other
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert_eq!(snapshot.state(), controller_domain::ExecutionState::Unknown);
    assert!(snapshot.receipt().is_some());
    assert!(
        scheduler
            .begin_osdeploy_pve_dispatch(&r.grant, snapshot.revision(), r.event, &r.request)
            .await
            .is_err()
    );
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}

#[tokio::test]
async fn task6_decision_rechecks_final_freshness_and_current_authority() {
    use postgres_store::OsDeployExecutionError as E;
    let s = osdeploy_execution_support::Scenario::with_freshness(30, true, 1).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let (event, revision) = s.observation(&r.grant).await;
    let before = s.db.snapshot().await;
    assert_eq!(
        s.db.other_scheduler()
            .decide_osdeploy_pve(&r.grant, revision, event)
            .await,
        Err(E::FenceLost)
    );
    assert_eq!(
        scheduler
            .decide_osdeploy_pve(&r.grant, revision - 1, event)
            .await,
        Err(E::FenceLost)
    );
    assert_eq!(s.db.snapshot().await, before);
    sqlx::raw_sql("CREATE FUNCTION decision_age() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(1.05); RETURN NEW; END $$; CREATE TRIGGER decision_age BEFORE INSERT ON rust_controller.osdeploy_decisions FOR EACH ROW EXECUTE FUNCTION decision_age();").execute(&s.db.pool).await.unwrap();
    assert_eq!(
        scheduler
            .decide_osdeploy_pve(&r.grant, revision, event)
            .await,
        Err(E::FenceLost)
    );
    assert_eq!(s.db.snapshot().await, before);
    sqlx::query("DROP TRIGGER decision_age ON rust_controller.osdeploy_decisions")
        .execute(&s.db.pool)
        .await
        .unwrap();
    assert_eq!(
        scheduler
            .decide_osdeploy_pve(&r.grant, revision, event)
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Unknown)
    );
    let reason: String = sqlx::query_scalar("SELECT payload_canonical_json::jsonb->'detail'->>'reason' FROM rust_controller.osdeploy_decisions WHERE action='pve_evaluated'").fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(reason, "observation_not_fresh");
}
#[tokio::test]
async fn task6_resume_requires_real_basis_cap_original_attempt_and_cancellation_fence() {
    use postgres_store::OsDeployExecutionError as E;
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Waiting
    );
    let parked =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    let before = s.db.snapshot().await;
    assert!(matches!(
        scheduler
            .resume_osdeploy_bound(
                r.grant.operation_id(),
                controller_domain::AttemptId::new(),
                parked.revision(),
                s.ids.workflow_sha256(),
                1
            )
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        scheduler
            .resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                parked.revision() - 1,
                s.ids.workflow_sha256(),
                1
            )
            .await,
        Err(E::FenceLost)
    ));
    assert!(matches!(
        scheduler
            .resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                parked.revision(),
                s.ids.workflow_sha256(),
                0
            )
            .await,
        Err(E::Validation)
    ));
    assert!(matches!(
        scheduler
            .resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                parked.revision(),
                &"a".repeat(64),
                1
            )
            .await,
        Err(E::FenceLost)
    ));
    let stale = postgres_store::Scheduler::new(
        s.db.other.clone(),
        postgres_store::ExecutorKind::Rust,
        2,
        "next-generation",
    )
    .unwrap();
    assert!(matches!(
        stale
            .resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                parked.revision(),
                s.ids.workflow_sha256(),
                1
            )
            .await,
        Err(E::FenceLost)
    ));
    assert_eq!(s.db.snapshot().await, before);
    sqlx::query("UPDATE rust_controller.osdeploy_schedule_projection SET next_check_at=next_check_at-interval '1 second' WHERE operation_id=$1").bind(r.grant.operation_id().as_uuid()).execute(&s.db.pool).await.unwrap();
    let poisoned = s.db.snapshot().await;
    assert!(matches!(
        scheduler
            .resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                parked.revision(),
                s.ids.workflow_sha256(),
                1
            )
            .await,
        Err(E::Validation)
    ));
    assert_eq!(s.db.snapshot().await, poisoned);
    sqlx::query("UPDATE rust_controller.osdeploy_schedule_projection SET next_check_at=$2 WHERE operation_id=$1").bind(r.grant.operation_id().as_uuid()).bind(parked.next_check_at()).execute(&s.db.pool).await.unwrap();
    let other_plan = osdeploy_support::altered(|v| {
        v["vm"]["target_vmid"] = serde_json::json!(902);
        v["vm"]["uuid"] = serde_json::json!("88888888-8888-4888-8888-888888888888");
        v["vm"]["mac"] = serde_json::json!("02:00:00:00:00:02");
        v["names"]["requested_name"] = serde_json::json!("Another");
        v["names"]["windows_name"] = serde_json::json!("Another");
        v["names"]["expected_agent_id"] = serde_json::json!("agent-another");
    });
    let other_ids =
        s.db.store
            .enqueue_osdeploy(controller_domain::RunId::new(), &other_plan)
            .await
            .unwrap();
    let other_grant = scheduler
        .claim_osdeploy_bound(
            other_ids.operation(osdeploy_adapter::OsDeployStage::Clone),
            other_ids.workflow_sha256(),
            1,
        )
        .await
        .unwrap()
        .unwrap();
    tokio::time::timeout(
        std::time::Duration::from_secs(4),
        sqlx::query(
            "SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))",
        )
        .bind(parked.next_check_at())
        .execute(&s.db.pool),
    )
    .await
    .unwrap()
    .unwrap();
    let capped = s.db.snapshot().await;
    assert!(
        scheduler
            .resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                parked.revision(),
                s.ids.workflow_sha256(),
                1
            )
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(s.db.snapshot().await, capped);
    assert_eq!(other_grant.attempt_number(), 1);
    owned_cancellation_control(&s, &r.grant).await;
    let cancelled =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    let before = s.db.snapshot().await;
    assert!(matches!(
        scheduler
            .resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                cancelled.revision(),
                s.ids.workflow_sha256(),
                2
            )
            .await,
        Err(E::FenceLost)
    ));
    assert_eq!(s.db.snapshot().await, before);
}

#[tokio::test]
async fn task6_due_resume_at_original_deadline_never_renews_attempt_budget() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(2, true).await;
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Waiting
    );
    let parked =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert_eq!(parked.next_check_at(), parked.deadline_at());
    tokio::time::timeout(
        std::time::Duration::from_secs(4),
        sqlx::query(
            "SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))",
        )
        .bind(r.grant.deadline_at())
        .execute(&s.db.pool),
    )
    .await
    .unwrap()
    .unwrap();
    let before = s.db.snapshot().await;
    assert!(matches!(
        scheduler
            .resume_osdeploy_bound(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                parked.revision(),
                s.ids.workflow_sha256(),
                1
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    ));
    assert_eq!(s.db.snapshot().await, before);
    // Scope-expiry consumer belongs to Task7; refusal cannot fabricate a lease.
    assert_eq!(
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap()
            .state(),
        controller_domain::ExecutionState::Waiting
    );
}
#[tokio::test]
async fn task6_expired_scope_wins_over_unauthorized_observation() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(2, true).await;
    let grant = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    s.fake.enqueue_provisioning_config_read(
        NodeName::parse("node-a").unwrap(),
        Vmid::new(900).unwrap(),
        FakeProvisioningConfigReadV1::Error(PveReadError::Unauthorized),
    );
    let (event, revision) = s.observation(&grant).await;
    tokio::time::timeout(
        std::time::Duration::from_secs(4),
        sqlx::query(
            "SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))",
        )
        .bind(grant.deadline_at())
        .execute(&s.db.pool),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(
        s.db.scheduler()
            .decide_osdeploy_pve(&grant, revision, event)
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Unknown)
    );
    let snapshot =
        s.db.store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
    assert_eq!(snapshot.state(), controller_domain::ExecutionState::Unknown);
    assert!(snapshot.dispatch().is_none());
}

#[tokio::test]
async fn task6_scope_crossing_during_decision_rolls_back_then_retries_original_expiry() {
    let s = osdeploy_execution_support::Scenario::new(2, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
        .await
        .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let (event, revision) = s.observation(&r.grant).await;
    sqlx::raw_sql("CREATE FUNCTION decision_cross_deadline() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(2.05); RETURN NEW; END $$; CREATE TRIGGER decision_cross_deadline BEFORE INSERT ON rust_controller.osdeploy_decisions FOR EACH ROW EXECUTE FUNCTION decision_cross_deadline();").execute(&s.db.pool).await.unwrap();
    let before = s.db.snapshot().await;
    assert_eq!(
        scheduler
            .decide_osdeploy_pve(&r.grant, revision, event)
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    );
    assert_eq!(s.db.snapshot().await, before);
    sqlx::query("DROP TRIGGER decision_cross_deadline ON rust_controller.osdeploy_decisions")
        .execute(&s.db.pool)
        .await
        .unwrap();
    assert_eq!(
        scheduler
            .decide_osdeploy_pve(&r.grant, revision, event)
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Unknown)
    );
    let before = s.db.snapshot().await;
    assert_eq!(
        scheduler
            .decide_osdeploy_pve(&r.grant, revision, event)
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Unknown)
    );
    assert_eq!(s.db.snapshot().await, before);
    let actions: Vec<String> = sqlx::query_scalar(
        "SELECT action FROM rust_controller.osdeploy_decisions WHERE resolution='unknown'",
    )
    .fetch_all(&s.db.pool)
    .await
    .unwrap();
    assert_eq!(actions, vec!["activated_scope_expired"]);
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}
#[tokio::test]
async fn cancellation_fences_all_sixteen_without_manufactured_attempts() {
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let g = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    s.db.scheduler()
        .cancel_osdeploy_run(s.ids.run_id())
        .await
        .unwrap();
    for stage in osdeploy_adapter::OsDeployStage::ALL {
        let snap =
            s.db.store
                .load_osdeploy_operation(s.ids.operation(stage))
                .await
                .unwrap();
        assert!(snap.cancelled());
        assert_eq!(snap.state(), controller_domain::ExecutionState::Blocked);
        assert_eq!(
            snap.attempt_id(),
            (stage == osdeploy_adapter::OsDeployStage::Clone).then_some(g.attempt_id())
        );
    }
    let before = s.db.snapshot().await;
    s.db.scheduler()
        .cancel_osdeploy_run(s.ids.run_id())
        .await
        .unwrap();
    assert_eq!(s.db.snapshot().await, before);
}
#[tokio::test]
async fn recovery_reclaims_twice_without_replacing_original_attempt_or_budget() {
    use osdeploy_adapter::OsDeployStage;
    let s = osdeploy_execution_support::Scenario::new(120, true).await;
    let op = s.ids.operation(OsDeployStage::Clone);
    let original =
        s.db.scheduler()
            .claim_osdeploy_bound(op, s.ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .unwrap();
    let activation =
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .activated_at();
    let mut grant = original.clone();
    for _ in 0..2 {
        s.wait_until(*grant.lease_expires_at()).await;
        let result = s.db.scheduler().reap_osdeploy_expired().await.unwrap();
        assert_eq!(
            (result.examined(), result.changed(), result.rejected()),
            (1, 1, 0)
        );
        let snap = s.db.store.load_osdeploy_operation(op).await.unwrap();
        assert_eq!(snap.state(), controller_domain::ExecutionState::Pending);
        assert_eq!(snap.attempt_id(), Some(original.attempt_id()));
        assert_eq!(snap.activated_at(), activation);
        assert_eq!(snap.deadline_at(), Some(*original.deadline_at()));
        grant =
            s.db.other_scheduler()
                .claim_osdeploy_bound(op, s.ids.workflow_sha256(), 1)
                .await
                .unwrap()
                .unwrap();
        assert_eq!(grant.attempt_id(), original.attempt_id());
        assert_eq!(grant.attempt_number(), 1);
        assert_eq!(grant.deadline_at(), original.deadline_at());
    }
    s.db.other_scheduler()
        .start_osdeploy_bound(&grant, s.ids.workflow_sha256())
        .await
        .unwrap();
    let starts: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='attempt_started'").bind(op.as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(starts, 1);
}

#[tokio::test]
async fn recovery_reparks_actual_read_only_running_and_resumed_waiting() {
    let s = osdeploy_execution_support::Scenario::new(120, true).await;
    let grant = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    s.wait_until(*grant.lease_expires_at()).await;
    let result = s.db.scheduler().reap_osdeploy_expired().await.unwrap();
    assert_eq!(
        (result.examined(), result.changed(), result.rejected()),
        (1, 1, 0)
    );
    let snap =
        s.db.store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
    assert_eq!(snap.state(), controller_domain::ExecutionState::Waiting);
    s.wait_until(snap.next_check_at().unwrap()).await;
    let resumed =
        s.db.scheduler()
            .resume_osdeploy_bound(
                grant.operation_id(),
                grant.attempt_id(),
                snap.revision(),
                s.ids.workflow_sha256(),
                1,
            )
            .await
            .unwrap()
            .unwrap();
    s.wait_until(*resumed.lease_expires_at()).await;
    assert_eq!(
        s.db.scheduler()
            .reap_osdeploy_expired()
            .await
            .unwrap()
            .changed(),
        1
    );
    let snap =
        s.db.store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
    assert_eq!(snap.state(), controller_domain::ExecutionState::Waiting);
    assert_eq!(snap.attempt_id(), Some(grant.attempt_id()));
    assert_eq!(snap.deadline_at(), Some(*grant.deadline_at()));
    assert!(snap.next_check_at().is_some());
}

#[tokio::test]
async fn recovery_dispatched_expired_observer_becomes_unknown_without_resend() {
    let s = osdeploy_execution_support::Scenario::new(120, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let (permit, _capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    drop(permit);
    s.wait_until(*r.grant.lease_expires_at()).await;
    assert_eq!(
        s.db.scheduler()
            .reap_osdeploy_expired()
            .await
            .unwrap()
            .changed(),
        1
    );
    let snap =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert_eq!(snap.state(), controller_domain::ExecutionState::Unknown);
    assert!(snap.dispatch().is_some());
    assert!(snap.receipt().is_none());
    assert!(snap.next_check_at().is_none());
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    assert!(
        s.db.scheduler()
            .claim_osdeploy_bound(r.grant.operation_id(), s.ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .is_none()
    );
}
#[tokio::test]
async fn task7_original_unknown_reconciles_actual_success_and_failure_without_new_lease() {
    use pve_port::*;
    for failed in [false, true] {
        let s = osdeploy_execution_support::Scenario::new(120, true).await;
        if failed {
            s.fake
                .enqueue_provisioning_outcome(
                    ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
                    FakeMutationOutcome::AcceptedTaskFails,
                )
                .unwrap();
        }
        let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
        let scheduler = s.db.scheduler();
        let (permit, capture) = scheduler
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
        let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
        assert_eq!(
            s.observe_and_decide(&r.grant).await,
            controller_domain::ExecutionState::Unknown
        );
        scheduler
            .record_osdeploy_pve_receipt(&capture, &receipt)
            .await
            .unwrap();
        let (event, revision) = s.reconciliation_observation(&r.grant).await;
        assert_eq!(
            scheduler
                .reconcile_osdeploy_unknown(
                    r.grant.operation_id(),
                    r.grant.attempt_id(),
                    revision,
                    event,
                    s.ids.workflow_sha256()
                )
                .await
                .unwrap(),
            postgres_store::OsDeployProgress::Decided(if failed {
                controller_domain::ExecutionState::Failed
            } else {
                controller_domain::ExecutionState::Satisfied
            })
        );
        let snap =
            s.db.store
                .load_osdeploy_operation(r.grant.operation_id())
                .await
                .unwrap();
        assert_eq!(snap.attempt_id(), Some(r.grant.attempt_id()));
        assert_eq!(snap.deadline_at(), Some(*r.grant.deadline_at()));
        let leases: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.worker_leases")
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
        assert_eq!(leases, 0);
        assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
        if !failed {
            assert_eq!(
                s.finish_stage(osdeploy_adapter::OsDeployStage::DiskCapacity)
                    .await,
                controller_domain::ExecutionState::Satisfied
            );
            assert_eq!(
                s.finish_stage(osdeploy_adapter::OsDeployStage::ConfigurePe)
                    .await,
                controller_domain::ExecutionState::Satisfied
            );
            assert_eq!(s.fake.recorded_provisioning_submissions().len(), 3);
        }
    }
}

#[tokio::test]
async fn task7_expiry_without_projection_records_once_at_original_deadline() {
    let s = osdeploy_execution_support::Scenario::new(1, true).await;
    let g = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    s.wait_until(*g.deadline_at()).await;
    let mut cursor = postgres_store::OsDeployExpiryCursor::default();
    let result =
        s.db.scheduler()
            .expire_osdeploy_scopes(&mut cursor)
            .await
            .unwrap();
    assert_eq!(
        (result.examined(), result.changed(), result.rejected()),
        (1, 1, 0)
    );
    let snap =
        s.db.store
            .load_osdeploy_operation(g.operation_id())
            .await
            .unwrap();
    assert_eq!(snap.state(), controller_domain::ExecutionState::Unknown);
    assert_eq!(snap.attempt_id(), Some(g.attempt_id()));
    let before = s.db.snapshot().await;
    assert_eq!(
        s.db.scheduler()
            .expire_osdeploy_scopes(&mut postgres_store::OsDeployExpiryCursor::default())
            .await
            .unwrap()
            .changed(),
        0
    );
    assert_eq!(before, s.db.snapshot().await);
}

#[tokio::test]
async fn task7_repair_and_due_restore_genuine_waiting_without_creating_authority() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(120, true).await;
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let (permit, capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    s.db.scheduler()
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Waiting
    );
    let snap =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    sqlx::query("DELETE FROM rust_controller.osdeploy_schedule_projection WHERE operation_id=$1")
        .bind(r.grant.operation_id().as_uuid())
        .execute(&s.db.pool)
        .await
        .unwrap();
    let mut cursor = postgres_store::OsDeployRepairCursor::default();
    assert_eq!(
        s.db.scheduler()
            .repair_osdeploy_schedules(&mut cursor)
            .await
            .unwrap()
            .changed(),
        1
    );
    s.wait_until(snap.next_check_at().unwrap()).await;
    let before = s.db.snapshot().await;
    let due = s.db.scheduler().discover_osdeploy_due().await.unwrap();
    assert_eq!(due.len(), 1);
    assert_eq!(due[0].operation_id(), r.grant.operation_id());
    assert_eq!(due[0].kind(), postgres_store::OsDeployDueKind::Waiting);
    assert_eq!(due[0].attempt_id(), r.grant.attempt_id());
    assert_eq!(before, s.db.snapshot().await);
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}
#[tokio::test]
async fn task7_terminal_residual_reaper_preserves_selected_outcome_and_original_attempt() {
    let s = osdeploy_execution_support::Scenario::new(120, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let lease: serde_json::Value = sqlx::query_scalar(
        "SELECT to_jsonb(l) FROM rust_controller.worker_leases l WHERE operation_id=$1",
    )
    .bind(r.grant.operation_id().as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    let (permit, _capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    drop(permit);
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Unknown
    );
    let attempts: serde_json::Value = sqlx::query_scalar(
        "SELECT to_jsonb(a) FROM rust_controller.attempts a WHERE attempt_id=$1",
    )
    .bind(r.grant.attempt_id().as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    sqlx::query("INSERT INTO rust_controller.worker_leases SELECT * FROM jsonb_populate_record(NULL::rust_controller.worker_leases,$1)").bind(lease).execute(&s.db.pool).await.unwrap();
    let summary = s.db.scheduler().reap_osdeploy_expired().await.unwrap();
    assert_eq!(
        (summary.examined(), summary.changed(), summary.rejected()),
        (1, 1, 0)
    );
    let after: serde_json::Value = sqlx::query_scalar(
        "SELECT to_jsonb(a) FROM rust_controller.attempts a WHERE attempt_id=$1",
    )
    .bind(r.grant.attempt_id().as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    assert_eq!(attempts, after);
    assert_eq!(
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap()
            .state(),
        controller_domain::ExecutionState::Unknown
    );
    let count:i64=sqlx::query_scalar("SELECT count(*) FROM rust_controller.osdeploy_decisions WHERE action='residual_lease_revoked'").fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(count, 1);
}
#[tokio::test]
async fn task7_unauthorized_unknown_reconciliation_is_validation_without_writes() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(120, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let (permit, capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Unknown
    );
    s.db.scheduler()
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    s.fake.enqueue_provisioning_config_read(
        NodeName::parse("node-a").unwrap(),
        Vmid::new(900).unwrap(),
        FakeProvisioningConfigReadV1::Error(PveReadError::Unauthorized),
    );
    let (event, revision) = s.reconciliation_observation(&r.grant).await;
    let before = s.db.snapshot().await;
    assert_eq!(
        s.db.scheduler()
            .reconcile_osdeploy_unknown(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                revision,
                event,
                s.ids.workflow_sha256()
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    );
    assert_eq!(before, s.db.snapshot().await);
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}
#[tokio::test]
async fn task7_cancel_terminal_unknown_clears_schedule_and_matching_future_lease() {
    let s = osdeploy_execution_support::Scenario::new(120, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let lease: serde_json::Value = sqlx::query_scalar(
        "SELECT to_jsonb(l) FROM rust_controller.worker_leases l WHERE operation_id=$1",
    )
    .bind(r.grant.operation_id().as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    let (permit, capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Unknown
    );
    s.db.scheduler()
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    s.db.scheduler()
        .repair_osdeploy_schedules(&mut postgres_store::OsDeployRepairCursor::default())
        .await
        .unwrap();
    assert!(
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap()
            .next_check_at()
            .is_some()
    );
    let selected:uuid::Uuid=sqlx::query_scalar("SELECT event_id FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND resolution='unknown' ORDER BY decision_revision DESC LIMIT 1").bind(r.grant.operation_id().as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    let attempt: serde_json::Value = sqlx::query_scalar(
        "SELECT to_jsonb(a) FROM rust_controller.attempts a WHERE attempt_id=$1",
    )
    .bind(r.grant.attempt_id().as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    sqlx::query("INSERT INTO rust_controller.worker_leases SELECT * FROM jsonb_populate_record(NULL::rust_controller.worker_leases,$1)").bind(lease).execute(&s.db.pool).await.unwrap();
    s.db.scheduler()
        .cancel_osdeploy_run(s.ids.run_id())
        .await
        .unwrap();
    let snap =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    assert!(snap.cancelled());
    assert_eq!(snap.state(), controller_domain::ExecutionState::Unknown);
    assert!(snap.next_check_at().is_none());
    let after: serde_json::Value = sqlx::query_scalar(
        "SELECT to_jsonb(a) FROM rust_controller.attempts a WHERE attempt_id=$1",
    )
    .bind(r.grant.attempt_id().as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    assert_eq!(attempt, after);
    let same:uuid::Uuid=sqlx::query_scalar("SELECT event_id FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND resolution='unknown' ORDER BY decision_revision DESC LIMIT 1").bind(r.grant.operation_id().as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(selected, same);
    let leases: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.worker_leases")
        .fetch_one(&s.db.pool)
        .await
        .unwrap();
    let schedules: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.osdeploy_schedule_projection")
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
    assert_eq!((leases, schedules), (0, 0));
}

#[tokio::test]
async fn task7_expiry_cursor_crosses_poison_pages_and_wraps_without_starvation() {
    use osdeploy_adapter::OsDeployStage;
    let f = osdeploy_support::Fixture::new().await;
    let mut grants = Vec::new();
    for index in 0..35_u32 {
        let p = osdeploy_support::altered(|v| {
            v["policy"]["mutation_seconds"] = serde_json::json!(1);
            v["policy"]["evidence_freshness_seconds"] = serde_json::json!(1);
            v["vm"]["target_vmid"] = serde_json::json!(1000 + index);
            v["vm"]["uuid"] = serde_json::json!(format!("88888888-8888-4888-8888-{index:012}"));
            v["vm"]["mac"] = serde_json::json!(format!("02:00:00:01:{index:02X}:01"));
            v["names"]["requested_name"] = serde_json::json!(format!("Fleet{index}"));
            v["names"]["windows_name"] = serde_json::json!(format!("Fleet{index}"));
            v["names"]["expected_agent_id"] = serde_json::json!(format!("agent-fleet{index}"));
        });
        let ids = f
            .store
            .enqueue_osdeploy(controller_domain::RunId::new(), &p)
            .await
            .unwrap();
        let grant = f
            .scheduler()
            .claim_osdeploy_bound(
                ids.operation(OsDeployStage::Clone),
                ids.workflow_sha256(),
                40,
            )
            .await
            .unwrap()
            .unwrap();
        grants.push(grant);
    }
    // Extra attempt is an owned corruption input, never a successful predecessor.
    sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state) VALUES($1,$2,2,'pending')").bind(controller_domain::AttemptId::new().as_uuid()).bind(grants[0].operation_id().as_uuid()).execute(&f.pool).await.unwrap();
    tokio::time::timeout(
        std::time::Duration::from_secs(3),
        sqlx::query(
            "SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))",
        )
        .bind(grants.last().unwrap().deadline_at())
        .execute(&f.pool),
    )
    .await
    .unwrap()
    .unwrap();
    let mut cursor = postgres_store::OsDeployExpiryCursor::default();
    let first = f
        .scheduler()
        .expire_osdeploy_scopes(&mut cursor)
        .await
        .unwrap();
    assert_eq!(
        (first.examined(), first.changed(), first.rejected()),
        (32, 31, 1)
    );
    let second = f
        .scheduler()
        .expire_osdeploy_scopes(&mut cursor)
        .await
        .unwrap();
    assert_eq!(
        (second.examined(), second.changed(), second.rejected()),
        (3, 3, 0)
    );
    for grant in grants.iter().skip(1) {
        let snap = f
            .store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
        assert_eq!(snap.state(), controller_domain::ExecutionState::Unknown);
        assert_eq!(snap.attempt_id(), Some(grant.attempt_id()));
    }
    let before = f.snapshot().await;
    let restarted = f
        .scheduler()
        .expire_osdeploy_scopes(&mut postgres_store::OsDeployExpiryCursor::default())
        .await
        .unwrap();
    assert_eq!(
        (
            restarted.examined(),
            restarted.changed(),
            restarted.rejected()
        ),
        (1, 0, 1)
    );
    assert_eq!(before, f.snapshot().await);
    let wrapped = f
        .scheduler()
        .expire_osdeploy_scopes(&mut cursor)
        .await
        .unwrap();
    assert_eq!(
        (wrapped.examined(), wrapped.changed(), wrapped.rejected()),
        (1, 0, 1)
    );
}
fn task7_fleet_plan(index: u32, seconds: u32) -> osdeploy_adapter::OsDeployPlanV1 {
    osdeploy_support::altered(|v| {
        v["policy"]["mutation_seconds"] = serde_json::json!(seconds);
        v["policy"]["evidence_freshness_seconds"] = serde_json::json!(seconds.min(30));
        v["vm"]["target_vmid"] = serde_json::json!(2000 + index);
        v["vm"]["uuid"] = serde_json::json!(format!("99999999-9999-4999-8999-{index:012}"));
        v["vm"]["mac"] = serde_json::json!(format!("02:00:00:02:{index:02X}:01"));
        v["names"]["requested_name"] = serde_json::json!(format!("Repair{index}"));
        v["names"]["windows_name"] = serde_json::json!(format!("Repair{index}"));
        v["names"]["expected_agent_id"] = serde_json::json!(format!("agent-repair{index}"));
    })
}

#[tokio::test]
async fn task7_projection_repair_pages_real_reparked_history_and_bounds_combined_due() {
    let f = osdeploy_support::Fixture::new().await;
    let mut grants = Vec::new();
    for index in 0..35 {
        let ids = f
            .store
            .enqueue_osdeploy(
                controller_domain::RunId::new(),
                &task7_fleet_plan(index, 300),
            )
            .await
            .unwrap();
        let grant = f
            .scheduler()
            .claim_osdeploy_bound(
                ids.operation(osdeploy_adapter::OsDeployStage::Clone),
                ids.workflow_sha256(),
                40,
            )
            .await
            .unwrap()
            .unwrap();
        f.scheduler()
            .start_osdeploy_bound(&grant, ids.workflow_sha256())
            .await
            .unwrap();
        grants.push(grant);
    }
    tokio::time::timeout(
        std::time::Duration::from_secs(35),
        sqlx::query(
            "SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))",
        )
        .bind(grants.last().unwrap().lease_expires_at())
        .execute(&f.pool),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(
        f.scheduler()
            .reap_osdeploy_expired()
            .await
            .unwrap()
            .changed(),
        32
    );
    assert_eq!(
        f.scheduler()
            .reap_osdeploy_expired()
            .await
            .unwrap()
            .changed(),
        3
    );
    sqlx::query("DELETE FROM rust_controller.osdeploy_schedule_projection")
        .execute(&f.pool)
        .await
        .unwrap();
    let mut cursor = postgres_store::OsDeployRepairCursor::default();
    let first = f
        .scheduler()
        .repair_osdeploy_schedules(&mut cursor)
        .await
        .unwrap();
    assert_eq!(
        (first.examined(), first.changed(), first.rejected()),
        (32, 32, 0)
    );
    let second = f
        .scheduler()
        .repair_osdeploy_schedules(&mut cursor)
        .await
        .unwrap();
    assert_eq!(
        (second.examined(), second.changed(), second.rejected()),
        (3, 3, 0)
    );
    for grant in &grants {
        assert_eq!(
            f.store
                .load_osdeploy_operation(grant.operation_id())
                .await
                .unwrap()
                .state(),
            controller_domain::ExecutionState::Waiting
        );
    }
    let due_at: chrono::DateTime<chrono::Utc> = sqlx::query_scalar(
        "SELECT max(next_check_at) FROM rust_controller.osdeploy_schedule_projection",
    )
    .fetch_one(&f.pool)
    .await
    .unwrap();
    tokio::time::timeout(
        std::time::Duration::from_secs(4),
        sqlx::query(
            "SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))",
        )
        .bind(due_at)
        .execute(&f.pool),
    )
    .await
    .unwrap()
    .unwrap();
    let before = f.snapshot().await;
    let due = f.scheduler().discover_osdeploy_due().await.unwrap();
    assert_eq!(due.len(), 32);
    let expected:Vec<uuid::Uuid>=sqlx::query_scalar("SELECT operation_id FROM rust_controller.osdeploy_schedule_projection ORDER BY next_check_at,operation_id LIMIT 32").fetch_all(&f.pool).await.unwrap();
    assert_eq!(
        due.iter()
            .map(|d| d.operation_id().as_uuid())
            .collect::<Vec<_>>(),
        expected
    );
    assert!(
        due.iter()
            .all(|d| d.kind() == postgres_store::OsDeployDueKind::Waiting)
    );
    assert_eq!(f.snapshot().await, before);
    assert_eq!(
        f.scheduler()
            .repair_osdeploy_schedules(&mut cursor)
            .await
            .unwrap()
            .changed(),
        0
    );
}

#[tokio::test]
async fn task7_missing_receipt_repair_only_enables_original_bounded_observation() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(120, true).await;
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let (permit, capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Unknown
    );
    let mut cursor = postgres_store::OsDeployRepairCursor::default();
    assert_eq!(
        s.db.scheduler()
            .repair_osdeploy_schedules(&mut cursor)
            .await
            .unwrap()
            .changed(),
        1
    );
    assert!(
        s.db.scheduler()
            .discover_osdeploy_due()
            .await
            .unwrap()
            .is_empty()
    );
    let (event, revision) = s.reconciliation_observation(&r.grant).await;
    let before = s.db.snapshot().await;
    assert_eq!(
        s.db.scheduler()
            .reconcile_osdeploy_unknown(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                revision,
                event,
                s.ids.workflow_sha256()
            )
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Idle
    );
    assert_eq!(before, s.db.snapshot().await);
    s.db.scheduler()
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    assert!(
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap()
            .next_check_at()
            .is_none()
    );
    assert_eq!(
        s.db.scheduler()
            .repair_osdeploy_schedules(&mut cursor)
            .await
            .unwrap()
            .changed(),
        1
    );
    let snap =
        s.db.store
            .load_osdeploy_operation(r.grant.operation_id())
            .await
            .unwrap();
    s.wait_until(snap.next_check_at().unwrap()).await;
    let due = s.db.scheduler().discover_osdeploy_due().await.unwrap();
    assert_eq!(due.len(), 1);
    assert_eq!(
        due[0].kind(),
        postgres_store::OsDeployDueKind::UnknownReconciliation
    );
    let (event, revision) = s.reconciliation_observation(&r.grant).await;
    assert_eq!(
        s.db.scheduler()
            .reconcile_osdeploy_unknown(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                revision,
                event,
                s.ids.workflow_sha256()
            )
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Unknown)
    );
    let states:i64=sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='execution_state_changed' AND execution_state='waiting'").bind(r.grant.operation_id().as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(states, 0);
    let MutationReceipt::Task(upid) = receipt else {
        panic!("actual Clone task required")
    };
    s.fake.complete_provisioning_task(&upid).unwrap();
    let (event, revision) = s.reconciliation_observation(&r.grant).await;
    assert_eq!(
        s.db.scheduler()
            .reconcile_osdeploy_unknown(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                revision,
                event,
                s.ids.workflow_sha256()
            )
            .await
            .unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Satisfied)
    );
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}
#[tokio::test]
async fn task7_reconciliation_preserves_synchronous_loss_and_actual_conflict() {
    use pve_port::*;
    for conflict in [false, true] {
        let s = osdeploy_execution_support::Scenario::new(120, false).await;
        s.finish_stage(osdeploy_adapter::OsDeployStage::Clone).await;
        s.finish_stage(osdeploy_adapter::OsDeployStage::DiskCapacity)
            .await;
        let r = s.ready(osdeploy_adapter::OsDeployStage::ConfigurePe).await;
        s.fake
            .enqueue_provisioning_outcome(
                ProvisioningFaultSelectorV1::new(ProvisioningActionV1::ConfigurePe, None),
                FakeMutationOutcome::AppliedResponseLost,
            )
            .unwrap();
        let (permit, _capture) =
            s.db.scheduler()
                .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
                .await
                .unwrap();
        assert_eq!(
            permit.submit_fake_once(s.fake.as_ref()).await,
            Err(PveWriteError::OutcomeUnknown)
        );
        assert_eq!(
            s.observe_and_decide(&r.grant).await,
            controller_domain::ExecutionState::Unknown
        );
        if conflict {
            let vm = r.request.plan().expected().vm();
            let config = s
                .fake
                .provisioning_vm_config(vm.node(), vm.target_vmid())
                .await
                .unwrap();
            let mut value = serde_json::to_value(config).unwrap();
            value["cores"] = serde_json::json!(12);
            s.fake
                .replace_provisioning_vm(
                    serde_json::from_value(value).unwrap(),
                    PowerState::Stopped,
                )
                .unwrap();
        }
        let (event, revision) = s.reconciliation_observation(&r.grant).await;
        assert_eq!(
            s.db.scheduler()
                .reconcile_osdeploy_unknown(
                    r.grant.operation_id(),
                    r.grant.attempt_id(),
                    revision,
                    event,
                    s.ids.workflow_sha256()
                )
                .await
                .unwrap(),
            postgres_store::OsDeployProgress::Decided(if conflict {
                controller_domain::ExecutionState::Conflicted
            } else {
                controller_domain::ExecutionState::Satisfied
            })
        );
        assert!(
            s.db.store
                .load_osdeploy_operation(r.grant.operation_id())
                .await
                .unwrap()
                .receipt()
                .is_none()
        );
        assert_eq!(s.fake.recorded_provisioning_submissions().len(), 2);
    }
}

#[tokio::test]
async fn task7_late_unknown_success_cannot_pass_deadline_and_expiry_has_no_second_state_event() {
    let s = osdeploy_execution_support::Scenario::new(2, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let (permit, capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Unknown
    );
    s.wait_until(*r.grant.deadline_at()).await;
    s.db.scheduler()
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let (event, revision) = s.reconciliation_observation(&r.grant).await;
    let before = s.db.snapshot().await;
    assert_eq!(
        s.db.scheduler()
            .reconcile_osdeploy_unknown(
                r.grant.operation_id(),
                r.grant.attempt_id(),
                revision,
                event,
                s.ids.workflow_sha256()
            )
            .await,
        Err(postgres_store::OsDeployExecutionError::FenceLost)
    );
    assert_eq!(before, s.db.snapshot().await);
    let states:i64=sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='execution_state_changed'").bind(r.grant.operation_id().as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(
        s.db.scheduler()
            .expire_osdeploy_scopes(&mut postgres_store::OsDeployExpiryCursor::default())
            .await
            .unwrap()
            .changed(),
        1
    );
    let after:i64=sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='execution_state_changed'").bind(r.grant.operation_id().as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(states, after);
    let saved = s.db.snapshot().await;
    assert_eq!(
        s.db.scheduler()
            .expire_osdeploy_scopes(&mut postgres_store::OsDeployExpiryCursor::default())
            .await
            .unwrap()
            .examined(),
        0
    );
    assert_eq!(saved, s.db.snapshot().await);
}

#[tokio::test]
async fn task7_reclaimed_pending_expires_with_original_attempt() {
    let s = osdeploy_execution_support::Scenario::new(31, true).await;
    let op = s.ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let g =
        s.db.scheduler()
            .claim_osdeploy_bound(op, s.ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .unwrap();
    s.wait_until(*g.lease_expires_at()).await;
    assert_eq!(
        s.db.scheduler()
            .reap_osdeploy_expired()
            .await
            .unwrap()
            .changed(),
        1
    );
    assert_eq!(
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .state(),
        controller_domain::ExecutionState::Pending
    );
    s.wait_until(*g.deadline_at()).await;
    assert_eq!(
        s.db.scheduler()
            .expire_osdeploy_scopes(&mut postgres_store::OsDeployExpiryCursor::default())
            .await
            .unwrap()
            .changed(),
        1
    );
    let snap = s.db.store.load_osdeploy_operation(op).await.unwrap();
    assert_eq!(snap.state(), controller_domain::ExecutionState::Unknown);
    assert_eq!(snap.attempt_id(), Some(g.attempt_id()));
    assert_eq!(snap.deadline_at(), Some(*g.deadline_at()));
}

#[tokio::test]
async fn task7_foreign_residual_attempt_rejects_reaping_and_atomic_cancellation() {
    let s = osdeploy_execution_support::Scenario::new(120, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let lease: serde_json::Value = sqlx::query_scalar(
        "SELECT to_jsonb(l) FROM rust_controller.worker_leases l WHERE operation_id=$1",
    )
    .bind(r.grant.operation_id().as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    let (permit, _) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    drop(permit);
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Unknown
    );
    let foreign = controller_domain::AttemptId::new();
    sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state) VALUES($1,$2,2,'pending')").bind(foreign.as_uuid()).bind(r.grant.operation_id().as_uuid()).execute(&s.db.pool).await.unwrap();
    let mut lease = lease;
    lease["attempt_id"] = serde_json::json!(foreign);
    sqlx::query("INSERT INTO rust_controller.worker_leases SELECT * FROM jsonb_populate_record(NULL::rust_controller.worker_leases,$1)").bind(lease).execute(&s.db.pool).await.unwrap();
    let before = s.db.snapshot().await;
    let result = s.db.scheduler().reap_osdeploy_expired().await.unwrap();
    assert_eq!(
        (result.examined(), result.changed(), result.rejected()),
        (1, 0, 1)
    );
    assert_eq!(before, s.db.snapshot().await);
    assert_eq!(
        s.db.scheduler().cancel_osdeploy_run(s.ids.run_id()).await,
        Err(postgres_store::OsDeployExecutionError::Validation)
    );
    assert_eq!(before, s.db.snapshot().await);
}
#[tokio::test]
async fn task7_expiry_cursor_finds_late_visible_original_scope_after_wrap() {
    let f = osdeploy_support::Fixture::new().await;
    let mut grants = Vec::new();
    for index in 0..33 {
        let ids = f
            .store
            .enqueue_osdeploy(
                controller_domain::RunId::new(),
                &task7_fleet_plan(index, 12),
            )
            .await
            .unwrap();
        grants.push(
            f.scheduler()
                .claim_osdeploy_bound(
                    ids.operation(osdeploy_adapter::OsDeployStage::Clone),
                    ids.workflow_sha256(),
                    40,
                )
                .await
                .unwrap()
                .unwrap(),
        );
    }
    let late = f
        .store
        .enqueue_osdeploy(controller_domain::RunId::new(), &task7_fleet_plan(40, 1))
        .await
        .unwrap();
    let op = late.operation(osdeploy_adapter::OsDeployStage::Clone);
    let mut gate = f.pool.acquire().await.unwrap();
    sqlx::query("SELECT pg_advisory_lock(781337)")
        .execute(&mut *gate)
        .await
        .unwrap();
    sqlx::raw_sql(&format!("CREATE FUNCTION late_activation_visibility() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.operation_id='{}'::uuid THEN PERFORM pg_advisory_xact_lock(781337); END IF; RETURN NEW; END $$; CREATE CONSTRAINT TRIGGER late_activation_visibility AFTER INSERT ON rust_controller.osdeploy_lease_epochs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION late_activation_visibility();",op.as_uuid())).execute(&f.pool).await.unwrap();
    let scheduler = f.scheduler();
    let hash = late.workflow_sha256().to_owned();
    let mut worker = Box::pin(scheduler.claim_osdeploy_bound(op, &hash, 40));
    tokio::time::timeout(std::time::Duration::from_secs(3),async {
        loop {
            tokio::select! {
                _=&mut worker=>panic!("activation completed before visibility barrier"),
                result=sqlx::query_scalar::<_,bool>("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND objid=781337 AND NOT granted)").fetch_one(&f.pool)=>if result.unwrap(){break},
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    }).await.unwrap();
    let last = *grants.last().unwrap().deadline_at();
    tokio::time::timeout(
        std::time::Duration::from_secs(15),
        sqlx::query(
            "SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))",
        )
        .bind(last)
        .execute(&f.pool),
    )
    .await
    .unwrap()
    .unwrap();
    let invisible:bool=sqlx::query_scalar("SELECT NOT EXISTS(SELECT 1 FROM rust_controller.osdeploy_attempt_bindings WHERE operation_id=$1)").bind(op.as_uuid()).fetch_one(&f.pool).await.unwrap();
    assert!(invisible);
    let mut cursor = postgres_store::OsDeployExpiryCursor::default();
    let first = f
        .scheduler()
        .expire_osdeploy_scopes(&mut cursor)
        .await
        .unwrap();
    assert_eq!(
        (first.examined(), first.changed(), first.rejected()),
        (32, 32, 0)
    );
    sqlx::query("SELECT pg_advisory_unlock(781337)")
        .execute(&mut *gate)
        .await
        .unwrap();
    let late_grant = tokio::time::timeout(std::time::Duration::from_secs(3), worker)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    // This delayed commit makes history visible; the stale grant is never used
    // to start or send. Its original deadline sorts behind the retained cursor.
    assert!(late_grant.deadline_at() < grants[31].deadline_at());
    let snap = f.store.load_osdeploy_operation(op).await.unwrap();
    assert_eq!(snap.state(), controller_domain::ExecutionState::Leased);
    assert_eq!(snap.deadline_at(), Some(*late_grant.deadline_at()));
    let second = f
        .scheduler()
        .expire_osdeploy_scopes(&mut cursor)
        .await
        .unwrap();
    assert_eq!(
        (second.examined(), second.changed(), second.rejected()),
        (1, 1, 0)
    );
    assert_eq!(
        f.store.load_osdeploy_operation(op).await.unwrap().state(),
        controller_domain::ExecutionState::Leased
    );
    let wrapped = f
        .scheduler()
        .expire_osdeploy_scopes(&mut cursor)
        .await
        .unwrap();
    assert_eq!(
        (wrapped.examined(), wrapped.changed(), wrapped.rejected()),
        (1, 1, 0)
    );
    assert_eq!(
        f.store.load_osdeploy_operation(op).await.unwrap().state(),
        controller_domain::ExecutionState::Unknown
    );
    sqlx::raw_sql("DROP TRIGGER late_activation_visibility ON rust_controller.osdeploy_lease_epochs; DROP FUNCTION late_activation_visibility();").execute(&f.pool).await.unwrap();
}
async fn task7_fault_invoke(
    s: &osdeploy_execution_support::Scenario,
    r: &osdeploy_execution_support::Ready,
    kind: &str,
    evidence: Option<(controller_domain::EventId, i64)>,
) -> Result<(), postgres_store::OsDeployExecutionError> {
    match kind {
        "cancel" => s.db.scheduler().cancel_osdeploy_run(s.ids.run_id()).await,
        "expire" => {
            s.db.scheduler()
                .expire_osdeploy_scopes(&mut postgres_store::OsDeployExpiryCursor::default())
                .await
                .map(|_| ())
        }
        "repair" => {
            s.db.scheduler()
                .repair_osdeploy_schedules(&mut postgres_store::OsDeployRepairCursor::default())
                .await
                .map(|_| ())
        }
        "reap" => s.db.scheduler().reap_osdeploy_expired().await.map(|_| ()),
        "reconcile" => {
            let (event, revision) = evidence.unwrap();
            s.db.scheduler()
                .reconcile_osdeploy_unknown(
                    r.grant.operation_id(),
                    r.grant.attempt_id(),
                    revision,
                    event,
                    s.ids.workflow_sha256(),
                )
                .await
                .map(|_| ())
        }
        _ => panic!("unknown owned fault case"),
    }
}

#[tokio::test]
async fn task7_new_mutations_rollback_every_actual_row_write_and_deferred_commit() {
    use pve_port::*;
    for kind in ["cancel", "expire", "repair", "reap", "reconcile"] {
        let s =
            osdeploy_execution_support::Scenario::new(if kind == "expire" { 2 } else { 300 }, true)
                .await;
        if kind == "repair" {
            s.fake
                .enqueue_provisioning_outcome(
                    ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
                    FakeMutationOutcome::AcceptedTaskDelayed,
                )
                .unwrap();
        }
        let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
        let mut evidence = None;
        if kind == "expire" {
            s.wait_until(*r.grant.deadline_at()).await;
        }
        if matches!(kind, "repair" | "reap" | "reconcile") {
            let lease: serde_json::Value = sqlx::query_scalar(
                "SELECT to_jsonb(l) FROM rust_controller.worker_leases l WHERE operation_id=$1",
            )
            .bind(r.grant.operation_id().as_uuid())
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
            let (permit, capture) =
                s.db.scheduler()
                    .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
                    .await
                    .unwrap();
            let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
            if kind == "repair" {
                s.db.scheduler()
                    .record_osdeploy_pve_receipt(&capture, &receipt)
                    .await
                    .unwrap();
            }
            let outcome = s.observe_and_decide(&r.grant).await;
            assert_eq!(
                outcome,
                if kind == "repair" {
                    controller_domain::ExecutionState::Waiting
                } else {
                    controller_domain::ExecutionState::Unknown
                }
            );
            if kind == "repair" {
                sqlx::query("DELETE FROM rust_controller.osdeploy_schedule_projection")
                    .execute(&s.db.pool)
                    .await
                    .unwrap();
            }
            if kind == "reap" {
                sqlx::query("INSERT INTO rust_controller.worker_leases SELECT * FROM jsonb_populate_record(NULL::rust_controller.worker_leases,$1)").bind(lease).execute(&s.db.pool).await.unwrap();
            }
            if kind == "reconcile" {
                s.db.scheduler()
                    .record_osdeploy_pve_receipt(&capture, &receipt)
                    .await
                    .unwrap();
                evidence = Some(s.reconciliation_observation(&r.grant).await);
            }
        }
        sqlx::raw_sql("CREATE FUNCTION task7_fail_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned_task7_commit_fault'; END $$;").execute(&s.db.pool).await.unwrap();
        let commit_table = if kind == "repair" {
            "osdeploy_schedule_projection"
        } else {
            "osdeploy_decisions"
        };
        sqlx::query(&format!("CREATE CONSTRAINT TRIGGER task7_commit_fault AFTER INSERT ON rust_controller.{commit_table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION task7_fail_commit()")).execute(&s.db.pool).await.unwrap();
        let before = s.db.snapshot().await;
        assert_eq!(
            task7_fault_invoke(&s, &r, kind, evidence).await,
            Err(postgres_store::OsDeployExecutionError::StorageUnavailable),
            "kind={kind} deferred commit"
        );
        assert!(
            s.db.snapshot().await == before,
            "kind={kind} deferred commit changed snapshot"
        );
        sqlx::query(&format!(
            "DROP TRIGGER task7_commit_fault ON rust_controller.{commit_table}"
        ))
        .execute(&s.db.pool)
        .await
        .unwrap();
        sqlx::raw_sql("CREATE SEQUENCE task7_write_number; CREATE TABLE task7_fault(target bigint); INSERT INTO task7_fault VALUES(0); CREATE FUNCTION task7_fail_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF nextval('task7_write_number')=(SELECT target FROM task7_fault) THEN RAISE EXCEPTION 'owned_task7_write_fault'; END IF; IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW; END $$;").execute(&s.db.pool).await.unwrap();
        let tables = [
            "journal_events",
            "outbox",
            "osdeploy_decisions",
            "operations",
            "operation_projection",
            "attempts",
            "osdeploy_schedule_projection",
            "worker_leases",
            "osdeploy_run_cancellations",
        ];
        for table in tables {
            sqlx::query(&format!("CREATE TRIGGER task7_write_fault BEFORE INSERT OR UPDATE OR DELETE ON rust_controller.{table} FOR EACH ROW EXECUTE FUNCTION task7_fail_write()")).execute(&s.db.pool).await.unwrap();
        }
        let mut writes = None;
        for boundary in 1..=256_i64 {
            sqlx::query("UPDATE task7_fault SET target=$1")
                .bind(boundary)
                .execute(&s.db.pool)
                .await
                .unwrap();
            sqlx::query("SELECT setval('task7_write_number',1,false)")
                .execute(&s.db.pool)
                .await
                .unwrap();
            match task7_fault_invoke(&s, &r, kind, evidence).await {
                Err(postgres_store::OsDeployExecutionError::StorageUnavailable) => assert!(
                    s.db.snapshot().await == before,
                    "kind={kind} boundary={boundary} changed snapshot"
                ),
                Ok(()) => {
                    writes = Some(boundary - 1);
                    break;
                }
                Err(error) => {
                    panic!("unexpected fixed error kind={kind} boundary={boundary}: {error:?}")
                }
            }
        }
        let writes = writes.expect("owned writer exceeded256 row mutations");
        assert!(writes > 0);
        println!("task7_atomic_writes kind={kind} verified={writes} plus deferred_commit");
        for table in tables {
            sqlx::query(&format!(
                "DROP TRIGGER task7_write_fault ON rust_controller.{table}"
            ))
            .execute(&s.db.pool)
            .await
            .unwrap();
        }
        let snap =
            s.db.store
                .load_osdeploy_operation(r.grant.operation_id())
                .await
                .unwrap();
        assert_eq!(
            snap.state(),
            match kind {
                "cancel" => controller_domain::ExecutionState::Blocked,
                "repair" => controller_domain::ExecutionState::Waiting,
                "reconcile" => controller_domain::ExecutionState::Satisfied,
                _ => controller_domain::ExecutionState::Unknown,
            }
        );
    }
}

#[tokio::test]
async fn task7_cancellation_races_actual_dispatch_parking_receipt_and_collection() {
    use postgres_store::OsDeployExecutionError as E;
    use pve_port::*;
    for kind in ["dispatch", "park", "receipt", "collect"] {
        let s = osdeploy_execution_support::Scenario::new(300, true).await;
        if kind == "park" {
            s.fake
                .enqueue_provisioning_outcome(
                    ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
                    FakeMutationOutcome::AcceptedTaskDelayed,
                )
                .unwrap();
        }
        let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
        let scheduler = s.db.scheduler();
        let other = postgres_store::Scheduler::new(
            s.db.other.clone(),
            postgres_store::ExecutorKind::Rust,
            1,
            "osdeploy-worker",
        )
        .unwrap();
        let expected = match kind {
            "dispatch" => {
                let (cancel, dispatch) =
                    tokio::time::timeout(std::time::Duration::from_secs(10), async {
                        tokio::join!(
                            scheduler.cancel_osdeploy_run(s.ids.run_id()),
                            other.begin_osdeploy_pve_dispatch(
                                &r.grant, r.revision, r.event, &r.request
                            )
                        )
                    })
                    .await
                    .unwrap();
                cancel.unwrap();
                match dispatch {
                    Ok(handles) => {
                        drop(handles);
                        controller_domain::ExecutionState::Unknown
                    }
                    Err(E::FenceLost) => controller_domain::ExecutionState::Blocked,
                    Err(error) => panic!("unexpected dispatch race error: {error:?}"),
                }
            }
            "collect" => {
                let snap =
                    s.db.store
                        .load_osdeploy_operation(r.grant.operation_id())
                        .await
                        .unwrap();
                let context =
                    s.db.store
                        .load_osdeploy_pve_context(
                            r.grant.operation_id(),
                            snap.revision(),
                            ProvisioningEvaluationModeV1::Preflight,
                        )
                        .await
                        .unwrap();
                let (cancel, collection) =
                    tokio::time::timeout(std::time::Duration::from_secs(10), async {
                        tokio::join!(scheduler.cancel_osdeploy_run(s.ids.run_id()), async {
                            let evidence = s.collect(&context).await;
                            s.db.store
                                .record_osdeploy_pve_evidence(
                                    r.grant.operation_id(),
                                    r.grant.attempt_id(),
                                    snap.revision(),
                                    &evidence,
                                )
                                .await
                        })
                    })
                    .await
                    .unwrap();
                cancel.unwrap();
                assert!(matches!(collection, Ok(_) | Err(E::FenceLost)));
                controller_domain::ExecutionState::Blocked
            }
            _ => {
                let (permit, capture) = scheduler
                    .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
                    .await
                    .unwrap();
                let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
                if kind == "receipt" {
                    let (cancel, captured) =
                        tokio::time::timeout(std::time::Duration::from_secs(10), async {
                            tokio::join!(
                                scheduler.cancel_osdeploy_run(s.ids.run_id()),
                                other.record_osdeploy_pve_receipt(&capture, &receipt)
                            )
                        })
                        .await
                        .unwrap();
                    cancel.unwrap();
                    captured.unwrap();
                    assert!(
                        s.db.store
                            .load_osdeploy_operation(r.grant.operation_id())
                            .await
                            .unwrap()
                            .receipt()
                            .is_some()
                    );
                } else {
                    scheduler
                        .record_osdeploy_pve_receipt(&capture, &receipt)
                        .await
                        .unwrap();
                    let (event, revision) = s.observation(&r.grant).await;
                    let (cancel, parked) =
                        tokio::time::timeout(std::time::Duration::from_secs(10), async {
                            tokio::join!(
                                scheduler.cancel_osdeploy_run(s.ids.run_id()),
                                scheduler.decide_osdeploy_pve(&r.grant, revision, event)
                            )
                        })
                        .await
                        .unwrap();
                    cancel.unwrap();
                    assert!(matches!(
                        parked,
                        Ok(postgres_store::OsDeployProgress::Waiting) | Err(E::FenceLost)
                    ));
                }
                controller_domain::ExecutionState::Unknown
            }
        };
        let snap =
            s.db.store
                .load_osdeploy_operation(r.grant.operation_id())
                .await
                .unwrap();
        assert!(snap.cancelled());
        assert_eq!(snap.state(), expected);
        assert!(snap.next_check_at().is_none());
        assert_eq!(
            s.fake.recorded_provisioning_submissions().len(),
            usize::from(matches!(kind, "park" | "receipt"))
        );
        let before = s.db.snapshot().await;
        scheduler.cancel_osdeploy_run(s.ids.run_id()).await.unwrap();
        assert_eq!(before, s.db.snapshot().await);
    }
}
#[tokio::test]
async fn task7_already_elapsed_scope_wins_inside_atomic_cancellation() {
    let s = osdeploy_execution_support::Scenario::new(1, true).await;
    let grant = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    s.wait_until(*grant.deadline_at()).await;
    s.db.scheduler()
        .cancel_osdeploy_run(s.ids.run_id())
        .await
        .unwrap();
    for stage in osdeploy_adapter::OsDeployStage::ALL {
        let snap =
            s.db.store
                .load_osdeploy_operation(s.ids.operation(stage))
                .await
                .unwrap();
        assert!(snap.cancelled());
        assert_eq!(
            snap.state(),
            if stage == osdeploy_adapter::OsDeployStage::Clone {
                controller_domain::ExecutionState::Unknown
            } else {
                controller_domain::ExecutionState::Blocked
            }
        );
    }
    let actions:Vec<String>=sqlx::query_scalar("SELECT action FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND resolution='unknown'").bind(grant.operation_id().as_uuid()).fetch_all(&s.db.pool).await.unwrap();
    assert_eq!(actions, vec!["activated_scope_expired"]);
}

#[tokio::test]
async fn task7_unavailable_reconciliation_backoff_is_bounded_and_usable_read_resets_it() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(120, true).await;
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let (permit, capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Unknown
    );
    s.db.scheduler()
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    for (count, seconds) in [(1_i16, 2_i64), (2, 4), (3, 8), (4, 10), (4, 10), (0, 5)] {
        if count > 0 {
            s.fake.enqueue_provisioning_config_read(
                NodeName::parse("node-a").unwrap(),
                Vmid::new(900).unwrap(),
                FakeProvisioningConfigReadV1::Error(PveReadError::TimedOut),
            );
        }
        let (event, revision) = s.reconciliation_observation(&r.grant).await;
        assert_eq!(
            s.db.scheduler()
                .reconcile_osdeploy_unknown(
                    r.grant.operation_id(),
                    r.grant.attempt_id(),
                    revision,
                    event,
                    s.ids.workflow_sha256()
                )
                .await
                .unwrap(),
            postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Unknown)
        );
        let (actual,delay):(i16,i64)=sqlx::query_as("SELECT p.unavailable_count,extract(epoch FROM (p.next_check_at-d.evaluated_at))::bigint FROM rust_controller.osdeploy_schedule_projection p JOIN rust_controller.osdeploy_decisions d ON d.event_id=p.basis_event_id WHERE p.operation_id=$1").bind(r.grant.operation_id().as_uuid()).fetch_one(&s.db.pool).await.unwrap();
        assert_eq!((actual, delay), (count, seconds));
    }
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
}
#[tokio::test]
async fn task7_projection_repair_does_not_repair_poisoned_immutable_epoch() {
    use pve_port::*;
    let s = osdeploy_execution_support::Scenario::new(120, true).await;
    s.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let (permit, capture) =
        s.db.scheduler()
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
    let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
    s.db.scheduler()
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    assert_eq!(
        s.observe_and_decide(&r.grant).await,
        controller_domain::ExecutionState::Waiting
    );
    sqlx::query("DELETE FROM rust_controller.osdeploy_schedule_projection")
        .execute(&s.db.pool)
        .await
        .unwrap();
    let mut tx = s.db.pool.begin().await.unwrap();
    sqlx::query(
        "ALTER TABLE rust_controller.osdeploy_lease_epochs DISABLE TRIGGER osdeploy_no_mutation",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    sqlx::query("UPDATE rust_controller.osdeploy_lease_epochs SET worker_id='owned-poison' WHERE operation_id=$1").bind(r.grant.operation_id().as_uuid()).execute(&mut *tx).await.unwrap();
    sqlx::query(
        "ALTER TABLE rust_controller.osdeploy_lease_epochs ENABLE TRIGGER osdeploy_no_mutation",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    tx.commit().await.unwrap();
    let before = s.db.snapshot().await;
    let result =
        s.db.scheduler()
            .repair_osdeploy_schedules(&mut postgres_store::OsDeployRepairCursor::default())
            .await
            .unwrap();
    assert_eq!(
        (result.examined(), result.changed(), result.rejected()),
        (1, 0, 1)
    );
    assert_eq!(before, s.db.snapshot().await);
    let enabled:bool=sqlx::query_scalar("SELECT tgenabled='O' FROM pg_trigger WHERE tgrelid='rust_controller.osdeploy_lease_epochs'::regclass AND tgname='osdeploy_no_mutation'").fetch_one(&s.db.pool).await.unwrap();
    assert!(enabled);
}

#[tokio::test]
async fn task7_reconciliation_rechecks_original_fences_and_final_scope_before_commit() {
    use postgres_store::OsDeployExecutionError as E;
    for final_scope in [false, true] {
        let s = osdeploy_execution_support::Scenario::new(if final_scope { 5 } else { 120 }, true)
            .await;
        let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
        let scheduler = s.db.scheduler();
        let (permit, capture) = scheduler
            .begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request)
            .await
            .unwrap();
        let receipt = permit.submit_fake_once(s.fake.as_ref()).await.unwrap();
        assert_eq!(
            s.observe_and_decide(&r.grant).await,
            controller_domain::ExecutionState::Unknown
        );
        scheduler
            .record_osdeploy_pve_receipt(&capture, &receipt)
            .await
            .unwrap();
        let (event, revision) = s.reconciliation_observation(&r.grant).await;
        let before = s.db.snapshot().await;
        if final_scope {
            sqlx::query("CREATE SEQUENCE task7_reconciliation_entered")
                .execute(&s.db.pool)
                .await
                .unwrap();
            sqlx::query("CREATE FUNCTION task7_delay_reconciliation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action='pve_evaluated' AND NEW.payload_canonical_json::jsonb->'detail'->>'mode'='reconciliation' THEN PERFORM nextval('task7_reconciliation_entered'); PERFORM pg_sleep(5); END IF; RETURN NEW; END $$").execute(&s.db.pool).await.unwrap();
            sqlx::query("CREATE TRIGGER task7_delay_reconciliation BEFORE INSERT ON rust_controller.osdeploy_decisions FOR EACH ROW EXECUTE FUNCTION task7_delay_reconciliation()").execute(&s.db.pool).await.unwrap();
            let unexpired: bool = sqlx::query_scalar("SELECT clock_timestamp()<$1")
                .bind(r.grant.deadline_at())
                .fetch_one(&s.db.pool)
                .await
                .unwrap();
            assert!(unexpired);
            let result = tokio::time::timeout(
                std::time::Duration::from_secs(10),
                scheduler.reconcile_osdeploy_unknown(
                    r.grant.operation_id(),
                    r.grant.attempt_id(),
                    revision,
                    event,
                    s.ids.workflow_sha256(),
                ),
            )
            .await
            .unwrap();
            assert_eq!(result, Err(E::FenceLost));
            let entered: (i64, bool) =
                sqlx::query_as("SELECT last_value,is_called FROM task7_reconciliation_entered")
                    .fetch_one(&s.db.pool)
                    .await
                    .unwrap();
            assert_eq!(
                entered,
                (1, true),
                "must reach the actual reconciliation write before final expiry"
            );
            assert_eq!(before, s.db.snapshot().await);
            sqlx::query(
                "DROP TRIGGER task7_delay_reconciliation ON rust_controller.osdeploy_decisions",
            )
            .execute(&s.db.pool)
            .await
            .unwrap();
            sqlx::query("DROP FUNCTION task7_delay_reconciliation()")
                .execute(&s.db.pool)
                .await
                .unwrap();
            sqlx::query("DROP SEQUENCE task7_reconciliation_entered")
                .execute(&s.db.pool)
                .await
                .unwrap();
        } else {
            for (attempt, cas, hash) in [
                (
                    controller_domain::AttemptId::new(),
                    revision,
                    s.ids.workflow_sha256(),
                ),
                (r.grant.attempt_id(), revision - 1, s.ids.workflow_sha256()),
                (
                    r.grant.attempt_id(),
                    revision,
                    "0000000000000000000000000000000000000000000000000000000000000000",
                ),
            ] {
                assert_eq!(
                    scheduler
                        .reconcile_osdeploy_unknown(
                            r.grant.operation_id(),
                            attempt,
                            cas,
                            event,
                            hash
                        )
                        .await,
                    Err(E::FenceLost)
                );
                assert_eq!(before, s.db.snapshot().await);
            }
            sqlx::query("UPDATE rust_controller.orchestration_authority SET generation=2")
                .execute(&s.db.pool)
                .await
                .unwrap();
            let before = s.db.snapshot().await;
            assert_eq!(
                scheduler
                    .reconcile_osdeploy_unknown(
                        r.grant.operation_id(),
                        r.grant.attempt_id(),
                        revision,
                        event,
                        s.ids.workflow_sha256()
                    )
                    .await,
                Err(E::FenceLost)
            );
            assert_eq!(before, s.db.snapshot().await);
            let current = postgres_store::Scheduler::new(
                s.db.other.clone(),
                postgres_store::ExecutorKind::Rust,
                2,
                "current-observer",
            )
            .unwrap();
            current.cancel_osdeploy_run(s.ids.run_id()).await.unwrap();
            let before = s.db.snapshot().await;
            assert_eq!(
                current
                    .reconcile_osdeploy_unknown(
                        r.grant.operation_id(),
                        r.grant.attempt_id(),
                        revision,
                        event,
                        s.ids.workflow_sha256()
                    )
                    .await,
                Err(E::FenceLost)
            );
            assert_eq!(before, s.db.snapshot().await);
        }
        assert_eq!(s.fake.recorded_provisioning_submissions().len(), 1);
    }
}
