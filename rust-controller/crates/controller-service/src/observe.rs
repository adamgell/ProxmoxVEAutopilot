use anyhow::{Context, Result};
use api_compat::{JobEnvelope, normalize_job};
use serde_json::json;
use sqlx::{PgConnection, PgPool};

const BEGIN_READ_ONLY_SQL: &str = "BEGIN TRANSACTION READ ONLY";
const VERIFY_READ_ONLY_SQL: &str = "SELECT \
    current_setting('transaction_read_only') = 'on' \
    AND has_table_privilege(current_user, 'jobs', 'SELECT') \
    AND NOT has_table_privilege(current_user, 'jobs', 'INSERT') \
    AND NOT has_table_privilege(current_user, 'jobs', 'UPDATE') \
    AND NOT has_table_privilege(current_user, 'jobs', 'DELETE') \
    AND NOT has_table_privilege(current_user, 'jobs', 'TRUNCATE') \
    AND NOT EXISTS (SELECT 1 FROM pg_user WHERE usename = current_user AND usesuper)";
const OBSERVE_JOB_SQL: &str = "SELECT json_build_object(\
    'id', id, \
    'job_type', job_type, \
    'playbook', playbook, \
    'cmd', cmd_json, \
    'args', args_json, \
    'status', status\
)::text FROM jobs \
WHERE job_type = $1 AND status = 'pending' \
ORDER BY created_at ASC, id ASC LIMIT 1";

struct ObserveReadCapability<'a> {
    connection: &'a mut PgConnection,
}

pub(crate) async fn observe_once(pool: &PgPool) -> Result<String> {
    let mut connection = pool
        .acquire()
        .await
        .context("observe-mode database connection acquisition failed")?;
    sqlx::query(BEGIN_READ_ONLY_SQL)
        .execute(&mut *connection)
        .await
        .context("observe-mode read-only transaction start failed")?;

    let result = observe_in_transaction(ObserveReadCapability {
        connection: &mut connection,
    })
    .await;
    let rollback = sqlx::query("ROLLBACK").execute(&mut *connection).await;
    rollback.context("observe-mode transaction rollback failed")?;
    result
}

async fn observe_in_transaction(capability: ObserveReadCapability<'_>) -> Result<String> {
    let is_read_only: bool = sqlx::query_scalar(VERIFY_READ_ONLY_SQL)
        .fetch_one(&mut *capability.connection)
        .await
        .context("observe-mode role verification SELECT failed")?;
    anyhow::ensure!(
        is_read_only,
        "observe mode requires a non-superuser PostgreSQL role with SELECT-only jobs access"
    );

    let raw_job: Option<String> = sqlx::query_scalar(OBSERVE_JOB_SQL)
        .bind("test_long_sleep")
        .fetch_optional(&mut *capability.connection)
        .await
        .context("compatibility observation SELECT failed")?;
    let compatibility = raw_job
        .as_deref()
        .and_then(|raw_job| JobEnvelope::from_json_str(raw_job).ok())
        .and_then(|job| normalize_job(&job).ok())
        .and_then(|plan| plan.fingerprint().ok());
    let (result, fingerprint) = match compatibility {
        Some(fingerprint) => ("compatible", fingerprint.redacted()),
        None => ("rejected", "[redacted]".to_owned()),
    };

    serde_json::to_string(&json!({
        "compatibility": result,
        "fingerprint": fingerprint,
    }))
    .context("compatibility result serialization failed")
}

pub(crate) async fn run(database_url: &str) -> Result<String> {
    let pool = PgPool::connect(database_url)
        .await
        .context("observe-mode database connection failed")?;
    observe_once(&pool).await
}

#[cfg(test)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ObserverStep {
    BeginReadOnly,
    VerifyReadOnly,
    ReadPendingJob,
    Rollback,
}

#[cfg(test)]
#[derive(Debug, Eq, PartialEq)]
struct ObserverTrace {
    backend_pid: String,
    steps: Vec<ObserverStep>,
}

#[cfg(test)]
fn parse_observer_trace(logs: &str) -> std::result::Result<ObserverTrace, String> {
    const IDENTITY: &str = "observer_login task7_observe";
    let expected_steps = [
        ObserverStep::BeginReadOnly,
        ObserverStep::VerifyReadOnly,
        ObserverStep::ReadPendingJob,
        ObserverStep::Rollback,
    ];
    let mut statements: Vec<(String, ObserverStep)> = Vec::new();
    let mut parameter_detail_count = 0;

    for line in logs.lines().filter(|line| line.contains(IDENTITY)) {
        let (prefix, record) = line
            .split_once(&format!("] {IDENTITY} "))
            .ok_or_else(|| format!("unrecognized observer log prefix: {line}"))?;
        let backend_pid = prefix
            .rsplit_once('[')
            .map(|parts| parts.1)
            .filter(|pid| !pid.is_empty() && pid.bytes().all(|byte| byte.is_ascii_digit()))
            .ok_or_else(|| format!("invalid observer backend PID: {line}"))?;
        let record = record.trim_start();

        if let Some(detail) = record.strip_prefix("DETAIL:") {
            let detail = detail.trim_start();
            if statements.last().map(|entry| entry.1) != Some(ObserverStep::ReadPendingJob)
                || parameter_detail_count != 0
                || detail != "parameters: $1 = 'test_long_sleep'"
                || statements.last().map(|entry| entry.0.as_str()) != Some(backend_pid)
            {
                return Err(format!("unexpected observer detail record: {line}"));
            }
            parameter_detail_count += 1;
            continue;
        }

        let payload = record
            .strip_prefix("LOG:")
            .map(str::trim_start)
            .ok_or_else(|| format!("unrecognized observer log record: {line}"))?;
        let sql = if let Some(sql) = payload.strip_prefix("statement:") {
            sql.trim_start()
        } else if let Some(execute) = payload.strip_prefix("execute ") {
            let (name, sql) = execute
                .split_once(": ")
                .ok_or_else(|| format!("malformed prepared execute record: {line}"))?;
            if name.is_empty()
                || !name
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
            {
                return Err(format!("invalid prepared statement name: {line}"));
            }
            sql
        } else {
            return Err(format!("unknown observer LOG payload: {line}"));
        };

        let normalized = normalize_logged_sql(sql);
        let step = if normalized == normalize_logged_sql(BEGIN_READ_ONLY_SQL) {
            ObserverStep::BeginReadOnly
        } else if normalized == normalize_logged_sql(VERIFY_READ_ONLY_SQL) {
            ObserverStep::VerifyReadOnly
        } else if normalized == normalize_logged_sql(OBSERVE_JOB_SQL) {
            ObserverStep::ReadPendingJob
        } else if normalized == "ROLLBACK" {
            ObserverStep::Rollback
        } else {
            return Err(format!("unexpected observer SQL: {normalized}"));
        };
        statements.push((backend_pid.to_owned(), step));
    }

    let steps = statements.iter().map(|entry| entry.1).collect::<Vec<_>>();
    if steps != expected_steps || parameter_detail_count != 1 {
        return Err(format!(
            "observer trace did not match the exact transaction sequence: {steps:?}"
        ));
    }
    let backend_pid = statements
        .first()
        .map(|entry| entry.0.clone())
        .ok_or_else(|| "observer trace contained no statements".to_owned())?;
    if statements.iter().any(|entry| entry.0 != backend_pid) {
        return Err("observer trace used more than one backend PID".to_owned());
    }

    Ok(ObserverTrace { backend_pid, steps })
}

#[cfg(test)]
fn normalize_logged_sql(sql: &str) -> String {
    sql.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests {
    use std::{
        process::{Command, Stdio},
        time::Duration,
    };

    use sqlx::{PgPool, postgres::PgPoolOptions};

    use super::{
        BEGIN_READ_ONLY_SQL, OBSERVE_JOB_SQL, ObserverStep, VERIFY_READ_ONLY_SQL, observe_once,
        parse_observer_trace,
    };

    struct PostgresContainer {
        id: String,
    }

    impl PostgresContainer {
        async fn start() -> Self {
            let output = Command::new("docker")
                .args([
                    "run",
                    "--pull=never",
                    "--detach",
                    "--env",
                    "POSTGRES_PASSWORD=postgres",
                    "--env",
                    "POSTGRES_DB=observe_test",
                    "--publish",
                    "127.0.0.1::5432",
                    "postgres:16-alpine",
                    "-c",
                    "log_statement=all",
                    "-c",
                    "log_line_prefix=%m [%p] %u %a ",
                ])
                .stderr(Stdio::inherit())
                .output()
                .expect("docker must be installed for PostgreSQL observation tests");
            assert!(
                output.status.success(),
                "cached PostgreSQL image must start"
            );
            let id = String::from_utf8(output.stdout)
                .expect("container id must be UTF-8")
                .trim()
                .to_owned();
            let container = Self { id };
            container.wait_for_pool(&container.admin_dsn()).await;
            container
        }

        fn port(&self) -> String {
            let output = Command::new("docker")
                .args([
                    "inspect",
                    "--format",
                    "{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}",
                    &self.id,
                ])
                .output()
                .expect("docker inspect must run");
            assert!(output.status.success());
            String::from_utf8(output.stdout).unwrap().trim().to_owned()
        }

        fn admin_dsn(&self) -> String {
            format!(
                "postgresql://postgres:postgres@127.0.0.1:{}/observe_test",
                self.port()
            )
        }

        fn observer_dsn(&self) -> String {
            format!(
                "postgresql://observer_login:observe-only@127.0.0.1:{}/observe_test?application_name=task7_observe",
                self.port()
            )
        }

        async fn wait_for_pool(&self, dsn: &str) -> PgPool {
            for _ in 0..60 {
                if let Ok(pool) = PgPoolOptions::new().max_connections(4).connect(dsn).await {
                    return pool;
                }
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
            panic!("PostgreSQL did not become ready within 15 seconds");
        }

        fn logs(&self) -> String {
            let output = Command::new("docker")
                .args(["logs", &self.id])
                .output()
                .expect("docker logs must run");
            assert!(output.status.success());
            String::from_utf8(output.stderr).unwrap()
        }
    }

    impl Drop for PostgresContainer {
        fn drop(&mut self) {
            let _ = Command::new("docker")
                .args(["rm", "--force", &self.id])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
    }

    async fn fixture() -> (PostgresContainer, PgPool, PgPool) {
        let postgres = PostgresContainer::start().await;
        let admin = postgres.wait_for_pool(&postgres.admin_dsn()).await;
        sqlx::raw_sql(
            r#"
            CREATE TABLE jobs (
                id text PRIMARY KEY,
                job_type text NOT NULL,
                playbook text NOT NULL,
                cmd_json jsonb NOT NULL,
                args_json jsonb NOT NULL,
                status text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT clock_timestamp()
            );
            INSERT INTO jobs (id, job_type, playbook, cmd_json, args_json, status, created_at)
            VALUES (
                '20260904-deadbeef',
                'test_long_sleep',
                '/app/playbooks/_test_long_sleep.yml',
                '["ansible-playbook","/app/playbooks/_test_long_sleep.yml","-e","duration=5"]',
                '{"duration":"5"}',
                'pending',
                '2026-09-04T12:01:00Z'
            );
            INSERT INTO jobs (id, job_type, playbook, cmd_json, args_json, status, created_at)
            VALUES (
                '20260904-00000001',
                'test_long_sleep',
                '/app/playbooks/_test_long_sleep.yml',
                '["ansible-playbook","/app/playbooks/_test_long_sleep.yml","-e","duration=6"]',
                '{"duration":"6"}',
                'running',
                '2026-09-04T12:00:00Z'
            );
            INSERT INTO jobs (id, job_type, playbook, cmd_json, args_json, status, created_at)
            VALUES (
                '20260904-00000002',
                'test_long_sleep',
                '/app/playbooks/_test_long_sleep.yml',
                '["ansible-playbook","/app/playbooks/_test_long_sleep.yml","-e","duration=7"]',
                '{"duration":"7"}',
                'pending',
                '2026-09-04T12:02:00Z'
            );
            CREATE ROLE observer_login LOGIN PASSWORD 'observe-only';
            REVOKE ALL ON jobs FROM observer_login;
            GRANT SELECT ON jobs TO observer_login;
            DO $$
            DECLARE function_signature text;
            BEGIN
                FOR function_signature IN
                    SELECT p.oid::regprocedure::text
                    FROM pg_proc AS p
                    WHERE p.proname LIKE '%advisory%'
                LOOP
                    EXECUTE format(
                        'REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC, observer_login',
                        function_signature
                    );
                END LOOP;
            END $$;
            "#,
        )
        .execute(&admin)
        .await
        .unwrap();
        let observer = postgres.wait_for_pool(&postgres.observer_dsn()).await;
        (postgres, admin, observer)
    }

    #[tokio::test]
    async fn observe_uses_select_only_role_and_audits_zero_mutating_statements() {
        let (postgres, admin, observer) = fixture().await;
        let privileges: (bool, bool, bool, bool) = sqlx::query_as(
            "SELECT has_table_privilege(current_user, 'jobs', 'SELECT'), \
                    has_table_privilege(current_user, 'jobs', 'INSERT'), \
                    has_table_privilege(current_user, 'jobs', 'UPDATE'), \
                    has_table_privilege(current_user, 'jobs', 'DELETE')",
        )
        .fetch_one(&observer)
        .await
        .unwrap();
        assert_eq!(privileges, (true, false, false, false));

        let before: String = sqlx::query_scalar(
            "SELECT json_agg(row_to_json(j) ORDER BY created_at, id)::text FROM jobs AS j",
        )
        .fetch_one(&admin)
        .await
        .unwrap();
        let locks_before: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted",
        )
        .fetch_one(&admin)
        .await
        .unwrap();
        let log_offset = postgres.logs().len();
        let output = observe_once(&observer).await.unwrap();

        let after: String = sqlx::query_scalar(
            "SELECT json_agg(row_to_json(j) ORDER BY created_at, id)::text FROM jobs AS j",
        )
        .fetch_one(&admin)
        .await
        .unwrap();
        let locks_after: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted",
        )
        .fetch_one(&admin)
        .await
        .unwrap();
        assert_eq!(after, before);
        assert_eq!(locks_after, locks_before);
        assert_eq!(
            output,
            "{\"compatibility\":\"compatible\",\"fingerprint\":\"sha256:d53da5428e15…\"}"
        );
        for forbidden in [
            "20260904-deadbeef",
            "sleep_seconds",
            "ansible-playbook",
            "_test_long_sleep.yml",
        ] {
            assert!(!output.contains(forbidden));
        }

        let logs = postgres.logs();
        let trace = parse_observer_trace(&logs[log_offset..]).unwrap();
        assert!(!trace.backend_pid.is_empty());
        assert_eq!(
            trace.steps,
            [
                ObserverStep::BeginReadOnly,
                ObserverStep::VerifyReadOnly,
                ObserverStep::ReadPendingJob,
                ObserverStep::Rollback,
            ]
        );

        let denied_write = sqlx::query("INSERT INTO jobs (id, job_type, playbook, cmd_json, args_json, status) VALUES ('forbidden', 'synthetic_long_sleep', '_test_long_sleep.yml', '[]', '{}', 'pending')")
            .execute(&observer)
            .await;
        assert!(denied_write.is_err());
    }

    #[test]
    fn server_log_parser_handles_generic_execute_names_and_fails_closed() {
        let trace = [
            format!(
                "2026-09-04 [42] observer_login task7_observe LOG: execute prepared_a: {BEGIN_READ_ONLY_SQL}"
            ),
            format!(
                "2026-09-04 [42] observer_login task7_observe LOG: execute any_name_2: {VERIFY_READ_ONLY_SQL}"
            ),
            format!(
                "2026-09-04 [42] observer_login task7_observe LOG: execute q3: {OBSERVE_JOB_SQL}"
            ),
            "2026-09-04 [42] observer_login task7_observe DETAIL: parameters: $1 = 'test_long_sleep'".to_owned(),
            "2026-09-04 [42] observer_login task7_observe LOG: statement: ROLLBACK".to_owned(),
        ]
        .join("\n");
        assert!(parse_observer_trace(&trace).is_ok());

        let unknown = format!(
            "{trace}\n2026-09-04 [42] observer_login task7_observe NOTICE: unrecognized record"
        );
        assert!(parse_observer_trace(&unknown).is_err());

        let detail = "2026-09-04 [42] observer_login task7_observe DETAIL: parameters: $1 = 'test_long_sleep'";
        let duplicate_detail = trace.replace(detail, &format!("{detail}\n{detail}"));
        assert!(parse_observer_trace(&duplicate_detail).is_err());
        assert!(parse_observer_trace(&trace.replace(detail, "")).is_err());
        let without_detail = trace.replace(&format!("{detail}\n"), "");
        assert!(parse_observer_trace(&format!("{detail}\n{without_detail}")).is_err());
        assert!(parse_observer_trace(&format!("{without_detail}\n{detail}")).is_err());
        assert!(
            parse_observer_trace(&trace.replace(detail, &detail.replace("[42]", "[43]"))).is_err()
        );

        let extra_select = format!(
            "2026-09-04 [42] observer_login task7_observe LOG: statement: SELECT 1\n{trace}"
        );
        assert!(parse_observer_trace(&extra_select).is_err());
    }

    #[tokio::test]
    async fn observe_rejects_a_superuser_even_inside_the_read_only_transaction() {
        let (_postgres, admin, _observer) = fixture().await;
        let result = observe_once(&admin).await;

        assert!(result.is_err());
    }

    #[tokio::test]
    async fn observe_rejection_output_never_contains_unsafe_job_material() {
        let (_postgres, admin, observer) = fixture().await;
        sqlx::query("UPDATE jobs SET args_json = '{\"token\":\"sensitive-value\"}'")
            .execute(&admin)
            .await
            .unwrap();
        let output = observe_once(&observer).await.unwrap();

        assert_eq!(
            output,
            "{\"compatibility\":\"rejected\",\"fingerprint\":\"[redacted]\"}"
        );
        assert!(!output.contains("token"));
        assert!(!output.contains("sensitive-value"));
    }

    #[tokio::test]
    async fn observer_role_cannot_execute_any_advisory_lock_function() {
        let (_postgres, admin, observer) = fixture().await;
        let executable: Vec<bool> = sqlx::query_scalar(
            "SELECT has_function_privilege('observer_login', p.oid, 'EXECUTE') \
             FROM pg_proc AS p \
             WHERE p.proname LIKE '%advisory%' \
             ORDER BY p.oid",
        )
        .fetch_all(&admin)
        .await
        .unwrap();
        assert!(!executable.is_empty());
        assert!(executable.into_iter().all(|allowed| !allowed));

        let lock = sqlx::query_scalar::<_, bool>("SELECT pg_try_advisory_lock(7::bigint)")
            .fetch_one(&observer)
            .await;
        assert!(lock.is_err());
    }

    #[tokio::test]
    async fn observe_has_no_process_capability_and_leaves_no_child_process() {
        let (_postgres, _admin, observer) = fixture().await;
        let before = child_processes();

        let output = observe_once(&observer).await.unwrap();

        assert!(output.contains("\"compatibility\":\"compatible\""));
        assert_eq!(child_processes(), before);
    }

    fn child_processes() -> Vec<String> {
        let output = Command::new("pgrep")
            .args(["-P", &std::process::id().to_string()])
            .output()
            .expect("pgrep must be available for the runtime child-process check");
        if output.status.success() {
            String::from_utf8(output.stdout)
                .unwrap()
                .lines()
                .map(str::to_owned)
                .collect()
        } else {
            assert_eq!(output.status.code(), Some(1));
            Vec::new()
        }
    }
}
