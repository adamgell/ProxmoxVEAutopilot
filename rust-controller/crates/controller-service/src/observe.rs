use anyhow::{Context, Result};
use api_compat::{JobEnvelope, normalize_job};
use serde_json::json;
use sqlx::PgPool;

const VERIFY_READ_ONLY_SQL: &str = "SELECT current_setting('default_transaction_read_only') = 'on'";
const OBSERVE_JOB_SQL: &str = "SELECT json_build_object(\
    'id', id, \
    'job_type', job_type, \
    'playbook', playbook, \
    'cmd', cmd_json, \
    'args', args_json, \
    'status', status\
)::text FROM jobs WHERE job_type = $1 ORDER BY id LIMIT 1";

#[derive(Default)]
pub(crate) struct ObservationAudit {
    statements: Vec<&'static str>,
    #[cfg(test)]
    process_spawn_attempts: usize,
}

impl ObservationAudit {
    fn record_statement(&mut self, statement: &'static str) {
        self.statements.push(statement);
    }

    #[cfg(test)]
    fn statements(&self) -> &[&'static str] {
        &self.statements
    }

    #[cfg(test)]
    const fn process_spawn_attempts(&self) -> usize {
        self.process_spawn_attempts
    }
}

pub(crate) async fn observe_once(pool: &PgPool, audit: &mut ObservationAudit) -> Result<String> {
    audit.record_statement(VERIFY_READ_ONLY_SQL);
    let is_read_only: bool = sqlx::query_scalar(VERIFY_READ_ONLY_SQL)
        .fetch_one(pool)
        .await
        .context("observe-mode role verification SELECT failed")?;
    anyhow::ensure!(
        is_read_only,
        "observe mode requires a PostgreSQL role with default_transaction_read_only=on"
    );

    audit.record_statement(OBSERVE_JOB_SQL);
    let raw_job: String = sqlx::query_scalar(OBSERVE_JOB_SQL)
        .bind("synthetic_long_sleep")
        .fetch_optional(pool)
        .await
        .context("compatibility observation SELECT failed")?
        .context("no sanitized synthetic compatibility job is available")?;
    let compatibility = JobEnvelope::from_json_str(&raw_job)
        .ok()
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
    let mut audit = ObservationAudit::default();
    observe_once(&pool, &mut audit).await
}

#[cfg(test)]
mod tests {
    use std::{
        process::{Command, Stdio},
        time::Duration,
    };

    use sqlx::{PgPool, postgres::PgPoolOptions};

    use super::{ObservationAudit, observe_once};

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
            INSERT INTO jobs (id, job_type, playbook, cmd_json, args_json, status)
            VALUES (
                'synthetic-job-0001',
                'synthetic_long_sleep',
                '_test_long_sleep.yml',
                '["ansible-playbook","_test_long_sleep.yml","-e","sleep_seconds=5"]',
                '{"sleep_seconds":5}',
                'pending'
            );
            CREATE ROLE observer_login LOGIN PASSWORD 'observe-only';
            ALTER ROLE observer_login SET default_transaction_read_only = on;
            REVOKE ALL ON jobs FROM observer_login;
            GRANT SELECT ON jobs TO observer_login;
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
        let (_postgres, admin, observer) = fixture().await;
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

        let before: String = sqlx::query_scalar("SELECT row_to_json(j)::text FROM jobs AS j")
            .fetch_one(&admin)
            .await
            .unwrap();
        let locks_before: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted",
        )
        .fetch_one(&admin)
        .await
        .unwrap();
        let mut audit = ObservationAudit::default();

        let output = observe_once(&observer, &mut audit).await.unwrap();

        let after: String = sqlx::query_scalar("SELECT row_to_json(j)::text FROM jobs AS j")
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
        assert_eq!(audit.process_spawn_attempts(), 0);
        assert_eq!(audit.statements().len(), 2);
        assert!(
            audit
                .statements()
                .iter()
                .all(|statement| statement.trim_start().starts_with("SELECT "))
        );
        for forbidden in [
            "INSERT",
            "UPDATE",
            "DELETE",
            "CREATE",
            "ALTER",
            "DROP",
            "TRUNCATE",
            "GRANT",
            "REVOKE",
            "pg_advisory",
            "FOR UPDATE",
            "CALL ",
            "COPY ",
        ] {
            assert!(
                audit
                    .statements()
                    .iter()
                    .all(|statement| !statement.to_ascii_uppercase().contains(forbidden))
            );
        }
        assert_eq!(
            output,
            "{\"compatibility\":\"compatible\",\"fingerprint\":\"sha256:e7cfdb9b71c5…\"}"
        );
        for forbidden in [
            "synthetic-job-0001",
            "sleep_seconds",
            "ansible-playbook",
            "_test_long_sleep.yml",
        ] {
            assert!(!output.contains(forbidden));
        }

        let denied_write = sqlx::query("INSERT INTO jobs (id, job_type, playbook, cmd_json, args_json, status) VALUES ('forbidden', 'synthetic_long_sleep', '_test_long_sleep.yml', '[]', '{}', 'pending')")
            .execute(&observer)
            .await;
        assert!(denied_write.is_err());
    }

    #[tokio::test]
    async fn observe_rejects_a_connection_that_is_not_default_read_only() {
        let (_postgres, admin, _observer) = fixture().await;
        let mut audit = ObservationAudit::default();

        let result = observe_once(&admin, &mut audit).await;

        assert!(result.is_err());
        assert_eq!(audit.statements().len(), 1);
        assert_eq!(audit.process_spawn_attempts(), 0);
    }

    #[tokio::test]
    async fn observe_rejection_output_never_contains_unsafe_job_material() {
        let (_postgres, admin, observer) = fixture().await;
        sqlx::query("UPDATE jobs SET args_json = '{\"token\":\"sensitive-value\"}'")
            .execute(&admin)
            .await
            .unwrap();
        let mut audit = ObservationAudit::default();

        let output = observe_once(&observer, &mut audit).await.unwrap();

        assert_eq!(
            output,
            "{\"compatibility\":\"rejected\",\"fingerprint\":\"[redacted]\"}"
        );
        assert!(!output.contains("token"));
        assert!(!output.contains("sensitive-value"));
        assert_eq!(audit.statements().len(), 2);
        assert_eq!(audit.process_spawn_attempts(), 0);
    }
}
