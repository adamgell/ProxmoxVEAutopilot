#[allow(
    dead_code,
    reason = "shared registration support has other harness consumers"
)]
mod osdeploy_support;

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
