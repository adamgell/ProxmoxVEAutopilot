//! Private, explicitly admitted Linux test database ownership.

use super::process::{self, LinuxCommand};
use serde_json::{Value, json};
use std::time::{Duration, Instant};

const PG_IMAGE: &str = "sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777";
const RECEIPT_KEYS: &[&str] = &[
    "marker",
    "netns",
    "pg_id",
    "pg_image",
    "runner_image",
    "session",
    "source_git_sha",
    "system_identifier",
    "version",
];
pub(super) const OVERRIDES: &[&str] = &[
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "PGHOST",
    "PGHOSTADDR",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGPASSFILE",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGOPTIONS",
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Receipt {
    version: u16,
    session: String,
    pg_id: String,
    pg_image: String,
    runner_image: String,
    source_git_sha: String,
    system_identifier: u64,
    marker: String,
    netns: String,
}

#[cfg(target_os = "linux")]
mod owned_linux_tests {
    use super::super::{Backend, CLEANUP_BOUND, Container, SETUP_BOUND};
    use super::*;
    use sqlx::{PgPool, postgres::PgPoolOptions};

    async fn guard() -> Container {
        assert_eq!(
            std::env::var("PROXMOXVEAUTOPILOT_LINUX_TEST_DB").as_deref(),
            Ok("owned-v1")
        );
        let receipt = admit().await.unwrap();
        let nonce = controller_domain::RunId::new()
            .as_uuid()
            .simple()
            .to_string();
        Container {
            backend: Backend::Linux(pending(receipt, "native_test", &nonce).unwrap()),
            cleanup_result: None,
        }
    }
    fn database(guard: &mut Container) -> &mut LinuxDatabase {
        match &mut guard.backend {
            Backend::Linux(value) => value,
            Backend::Docker(_) => panic!("owned Linux fixture required"),
        }
    }
    async fn admin() -> PgPool {
        // Fresh admission precedes this fixed endpoint; no test accepts a DSN.
        admit().await.unwrap();
        PgPoolOptions::new().max_connections(1).acquire_timeout(Duration::from_secs(2))
            .connect("postgresql://postgres:local-linux-probe@127.0.0.1:5432/postgres?hostaddr=127.0.0.1").await.unwrap()
    }
    async fn row(pool: &PgPool, name: &str) -> Option<(i64, Option<String>)> {
        sqlx::query_as("SELECT oid::bigint, shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=$1")
            .bind(name).fetch_optional(pool).await.unwrap()
    }
    async fn fault(guard: &mut Container, fault: process::LinuxFault) -> process::FaultChild {
        let mut child = process::linux_fault(&database(guard).request(), fault).unwrap();
        child.ready().await.unwrap();
        child
    }
    fn reap(child: process::FaultChild) {
        let pid = child.id();
        let started = Instant::now();
        drop(child);
        process::assert_child_reaped(pid);
        eprintln!(
            "owned_linux_child_reaped pid={pid} elapsed_ms={}",
            started.elapsed().as_millis()
        );
        assert!(started.elapsed() < Duration::from_millis(600));
    }
    fn cleanup(guard: &mut Container, success: bool) {
        let started = Instant::now();
        let result = guard.cleanup();
        let elapsed = started.elapsed();
        eprintln!(
            "owned_linux_cleanup confirmed={} elapsed_ms={}",
            result.is_ok(),
            elapsed.as_millis()
        );
        assert_eq!(result.is_ok(), success);
        // Small scheduling allowance is reported separately from the fixed deadline.
        assert!(elapsed < CLEANUP_BOUND + Duration::from_millis(200));
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires the owned Linux fixture namespace"]
    async fn owned_linux_create_identity_independent_pools_and_open_pool_cleanup() {
        tokio::time::timeout(SETUP_BOUND, async {
            let mut guard = guard().await;
            let dsn = database(&mut guard).create().await.unwrap();
            let first = PgPoolOptions::new().max_connections(1).connect(&dsn).await.unwrap();
            let second = PgPoolOptions::new().max_connections(1).connect(&dsn).await.unwrap();
            let mut a=first.acquire().await.unwrap(); let mut b=second.acquire().await.unwrap();
            let identity: (i64, String) = sqlx::query_as("SELECT oid::bigint, shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=current_database()").fetch_one(&mut *a).await.unwrap();
            assert_eq!(identity, (i64::from(database(&mut guard).oid.unwrap()), database(&mut guard).marker.clone()));
            sqlx::query("CREATE TABLE fixture_probe (value integer)").execute(&mut *a).await.unwrap();
            sqlx::query("INSERT INTO fixture_probe VALUES (17)").execute(&mut *a).await.unwrap();
            assert_eq!(sqlx::query_scalar::<_,i32>("SELECT value FROM fixture_probe").fetch_one(&mut *b).await.unwrap(),17);
            cleanup(&mut guard, true);
            assert!(sqlx::query("SELECT 1").execute(&mut *a).await.is_err());
            assert!(sqlx::query("SELECT 1").execute(&mut *b).await.is_err());
            // A second call uses the recorded result, even if admission would now fail.
            database(&mut guard).receipt.netns = "net:[1]".into();
            cleanup(&mut guard, true);
        }).await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires the owned Linux fixture namespace"]
    async fn owned_linux_cancel_before_create_reaps_and_confirms_absence() {
        tokio::time::timeout(SETUP_BOUND, async {
            let mut guard = guard().await;
            let name = database(&mut guard).name.clone();
            let child = fault(&mut guard, process::LinuxFault::BeforeCreate).await;
            reap(child);
            cleanup(&mut guard, true);
            assert!(row(&admin().await, &name).await.is_none());
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires the owned Linux fixture namespace"]
    async fn owned_linux_setup_cancellation_drops_child_before_one_shot_guard_cleanup() {
        tokio::time::timeout(SETUP_BOUND, async {
            let mut guard = guard().await;
            let name = database(&mut guard).name.clone();
            let child = fault(&mut guard, process::LinuxFault::Stamped).await;
            let pid = child.id();
            let started = Instant::now();
            let cancelled = tokio::time::timeout(Duration::from_millis(40), async move {
                let _owned = guard;
                let _creating = child;
                std::future::pending::<()>().await;
            })
            .await;
            assert!(cancelled.is_err());
            assert!(started.elapsed() < CLEANUP_BOUND + Duration::from_millis(600));
            process::assert_child_reaped(pid);
            assert!(row(&admin().await, &name).await.is_none());
            eprintln!(
                "owned_linux_setup_cancelled pid={pid} cleanup_elapsed_ms={}",
                started.elapsed().as_millis()
            );
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires the owned Linux fixture namespace"]
    async fn owned_linux_cancel_unmarked_create_retains_unconfirmed_database() {
        tokio::time::timeout(SETUP_BOUND, async {
            let mut guard = guard().await;
            let name = database(&mut guard).name.clone();
            let child = fault(&mut guard, process::LinuxFault::Unmarked).await;
            reap(child);
            cleanup(&mut guard, false);
            assert_eq!(row(&admin().await, &name).await.unwrap().1, None);
            eprintln!("owned_linux_retained_unmarked_database={name}");
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires the owned Linux fixture namespace"]
    async fn owned_linux_lost_stamped_response_recovers_only_full_marker() {
        tokio::time::timeout(SETUP_BOUND, async {
            let mut guard = guard().await;
            let name = database(&mut guard).name.clone();
            let child = fault(&mut guard, process::LinuxFault::Stamped).await;
            reap(child);
            assert!(database(&mut guard).oid.is_none());
            cleanup(&mut guard, true);
            assert!(row(&admin().await, &name).await.is_none());
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires the owned Linux fixture namespace"]
    async fn owned_linux_active_create_lock_refuses_even_absent_name() {
        tokio::time::timeout(SETUP_BOUND, async {
            let mut guard = guard().await;
            let name = database(&mut guard).name.clone();
            let child = fault(&mut guard, process::LinuxFault::Locked).await;
            cleanup(&mut guard, false);
            reap(child);
            assert!(row(&admin().await, &name).await.is_none());
            cleanup(&mut guard, false);
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires the owned Linux fixture namespace"]
    async fn owned_linux_matching_marker_wrong_oid_retains_database() {
        tokio::time::timeout(SETUP_BOUND, async {
            let mut guard = guard().await;
            database(&mut guard).create().await.unwrap();
            let original = database(&mut guard).oid.unwrap();
            let name = database(&mut guard).name.clone();
            database(&mut guard).oid = Some(original.checked_add(1).unwrap());
            cleanup(&mut guard, false);
            assert_eq!(
                row(&admin().await, &name).await.unwrap(),
                (
                    i64::from(original),
                    Some(database(&mut guard).marker.clone())
                )
            );
            eprintln!("owned_linux_retained_oid_mismatch_database={name}");
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires the owned Linux fixture namespace"]
    async fn owned_linux_foreign_marker_retains_database() {
        tokio::time::timeout(SETUP_BOUND, async {
            let mut guard = guard().await;
            database(&mut guard).create().await.unwrap();
            let name = database(&mut guard).name.clone();
            let admin = admin().await;
            // Name was generated and validated by pending(); the comment is fixed.
            sqlx::query(&format!(
                "COMMENT ON DATABASE \"{name}\" IS 'foreign-test-marker'"
            ))
            .execute(&admin)
            .await
            .unwrap();
            cleanup(&mut guard, false);
            assert_eq!(
                row(&admin, &name).await.unwrap().1.as_deref(),
                Some("foreign-test-marker")
            );
            eprintln!("owned_linux_retained_foreign_marker_database={name}");
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires the owned Linux fixture namespace"]
    async fn owned_linux_foreign_receipt_and_namespace_refuse_mutation() {
        tokio::time::timeout(SETUP_BOUND, async {
            for namespace in [false, true] {
                let mut guard = guard().await;
                database(&mut guard).create().await.unwrap();
                let name = database(&mut guard).name.clone();
                let admin = admin().await;
                let before = row(&admin, &name).await.unwrap();
                if namespace {
                    database(&mut guard).receipt.netns = "net:[1]".into();
                } else {
                    database(&mut guard).receipt.pg_id = "0".repeat(64);
                }
                cleanup(&mut guard, false);
                assert_eq!(row(&admin, &name).await.unwrap(), before);
                eprintln!("owned_linux_retained_receipt_mismatch_database={name}");
            }
        })
        .await
        .unwrap();
    }
}
pub(super) struct LinuxDatabase {
    receipt: Receipt,
    family: String,
    nonce: String,
    name: String,
    marker: String,
    oid: Option<u32>,
}
pub(super) fn select_mode(is_linux: bool, value: Option<&str>) -> Result<bool, ()> {
    match value {
        None => Ok(false),
        Some("owned-v1") if is_linux => Ok(true),
        _ => Err(()),
    }
}
fn lowerhex(value: &str, size: usize) -> bool {
    value.len() == size
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn decimal(value: &str) -> Result<u64, ()> {
    if value.is_empty() || !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(());
    }
    let number = value.parse::<u64>().map_err(|_| ())?;
    if number.to_string() != value {
        return Err(());
    }
    Ok(number)
}
pub(super) fn database_name(family: &str, nonce: &str) -> Result<String, ()> {
    if !matches!(
        family,
        "native_proof" | "native_test" | "osdeploy_registration_test" | "rust_controller_test"
    ) || !lowerhex(nonce, 32)
    {
        return Err(());
    }
    let name = format!("lf_{family}_{nonce}");
    if name.len() > 63 {
        return Err(());
    }
    Ok(name)
}
pub(super) fn canonical(bytes: &[u8]) -> Result<Value, ()> {
    if bytes.len() > 8192 {
        return Err(());
    }
    let bytes = bytes.strip_suffix(b"\n").unwrap_or(bytes);
    let value: Value = serde_json::from_slice(bytes).map_err(|_| ())?;
    if serde_json::to_vec(&value).map_err(|_| ())? != bytes {
        return Err(());
    }
    Ok(value)
}
fn keys(value: &Value, names: &[&str]) -> Result<(), ()> {
    let object = value.as_object().ok_or(())?;
    if object.len() != names.len() || names.iter().any(|key| !object.contains_key(*key)) {
        return Err(());
    }
    Ok(())
}
fn version(value: &Value) -> Result<(), ()> {
    if value["version"].as_u64() != Some(1) {
        return Err(());
    }
    Ok(())
}
fn string<'a>(value: &'a Value, key: &str) -> Result<&'a str, ()> {
    value[key].as_str().ok_or(())
}
impl Receipt {
    fn parse(value: &Value) -> Result<Self, ()> {
        keys(value, RECEIPT_KEYS)?;
        version(value)?;
        if serde_json::to_vec(value).map_err(|_| ())?.len() > 4096 {
            return Err(());
        }
        let session = string(value, "session")?;
        let pg_id = string(value, "pg_id")?;
        let pg_image = string(value, "pg_image")?;
        let runner_image = string(value, "runner_image")?;
        let source_git_sha = string(value, "source_git_sha")?;
        let marker = string(value, "marker")?;
        let netns = string(value, "netns")?;
        let ns = netns
            .strip_prefix("net:[")
            .and_then(|s| s.strip_suffix(']'))
            .ok_or(())?;
        if !lowerhex(session, 32)
            || !lowerhex(pg_id, 64)
            || pg_image != PG_IMAGE
            || !runner_image
                .strip_prefix("sha256:")
                .is_some_and(|s| lowerhex(s, 64))
            || !lowerhex(source_git_sha, 40)
            || marker != format!("lf_{session}")
            || decimal(ns)? == 0
        {
            return Err(());
        }
        Ok(Self {
            version: 1,
            session: session.into(),
            pg_id: pg_id.into(),
            pg_image: pg_image.into(),
            runner_image: runner_image.into(),
            source_git_sha: source_git_sha.into(),
            system_identifier: decimal(string(value, "system_identifier")?)?,
            marker: marker.into(),
            netns: netns.into(),
        })
    }
    fn value(&self) -> Value {
        json!({"version":self.version,"session":self.session,"pg_id":self.pg_id,"pg_image":self.pg_image,"runner_image":self.runner_image,"source_git_sha":self.source_git_sha,"system_identifier":self.system_identifier.to_string(),"marker":self.marker,"netns":self.netns})
    }
}
pub(super) fn receipt_response(bytes: &[u8]) -> Result<Receipt, ()> {
    let value = canonical(bytes)?;
    keys(&value, &["receipt", "version"])?;
    version(&value)?;
    Receipt::parse(&value["receipt"])
}
fn overrides_absent(mut present: impl FnMut(&str) -> bool) -> Result<(), ()> {
    if OVERRIDES.iter().any(|key| present(key)) {
        return Err(());
    }
    Ok(())
}
#[cfg_attr(
    not(target_os = "linux"),
    allow(dead_code, reason = "actual admission is Linux test only")
)]
pub(super) async fn admit() -> Result<Receipt, ()> {
    overrides_absent(|key| std::env::var_os(key).is_some())?;
    let output = process::linux_python(
        LinuxCommand::Admit,
        r#"{"version":1}"#,
        Duration::from_secs(3),
    )
    .await
    .map_err(|_| ())?;
    if !output.status.success() {
        return Err(());
    }
    receipt_response(&output.stdout)
}
pub(super) fn pending(receipt: Receipt, family: &str, nonce: &str) -> Result<LinuxDatabase, ()> {
    let name = database_name(family, nonce)?;
    let marker = format!("lf:v1:{}:{family}:{nonce}", receipt.session);
    Ok(LinuxDatabase {
        receipt,
        family: family.into(),
        nonce: nonce.into(),
        name,
        marker,
        oid: None,
    })
}
impl LinuxDatabase {
    fn request(&self) -> String {
        json!({"version":1,"receipt":self.receipt.value(),"family":self.family,"nonce":self.nonce,"database":self.name,"marker":self.marker,"expected_oid":self.oid}).to_string()
    }
    fn accept_create(&mut self, bytes: &[u8]) -> Result<String, ()> {
        let value = canonical(bytes)?;
        keys(&value, &["database", "marker", "oid", "version"])?;
        version(&value)?;
        let oid = value["oid"]
            .as_u64()
            .and_then(|n| u32::try_from(n).ok())
            .filter(|n| *n > 0)
            .ok_or(())?;
        if string(&value, "database")? != self.name
            || string(&value, "marker")? != self.marker
            || self.oid.is_some()
        {
            return Err(());
        }
        self.oid = Some(oid);
        Ok(format!(
            "postgresql://postgres:local-linux-probe@127.0.0.1:5432/{}?hostaddr=127.0.0.1",
            self.name
        ))
    }
    #[cfg_attr(
        not(target_os = "linux"),
        allow(dead_code, reason = "actual database work is Linux test only")
    )]
    pub(super) async fn create(&mut self) -> Result<String, ()> {
        let output = process::linux_python(
            LinuxCommand::Create,
            &self.request(),
            Duration::from_secs(3),
        )
        .await
        .map_err(|_| ())?;
        if !output.status.success() {
            return Err(());
        }
        self.accept_create(&output.stdout)
    }
    fn accept_cleanup(&self, bytes: &[u8]) -> Result<(), ()> {
        let value = canonical(bytes)?;
        keys(&value, &["database", "status", "version"])?;
        version(&value)?;
        if string(&value, "database")? != self.name || string(&value, "status")? != "absent" {
            return Err(());
        }
        Ok(())
    }
    #[cfg_attr(
        not(target_os = "linux"),
        allow(dead_code, reason = "actual database work is Linux test only")
    )]
    pub(super) fn cleanup(&mut self, deadline: Instant) -> Result<(), ()> {
        let output = process::linux_python_cleanup(&self.request(), deadline).map_err(|_| ())?;
        if !output.status.success() {
            return Err(());
        }
        self.accept_cleanup(&output.stdout)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const GOOD: &str = r#"{"receipt":{"marker":"lf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","netns":"net:[12345]","pg_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","pg_image":"sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777","runner_image":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","session":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_git_sha":"dddddddddddddddddddddddddddddddddddddddd","system_identifier":"7682173388088000546","version":1},"version":1}"#;
    #[test]
    fn mode_is_explicit_and_platform_scoped() {
        assert_eq!(select_mode(true, None), Ok(false));
        assert_eq!(select_mode(true, Some("owned-v1")), Ok(true));
        assert!(select_mode(false, Some("owned-v1")).is_err());
        assert!(select_mode(true, Some("other")).is_err());
    }
    #[test]
    fn database_identity_is_generated_and_bounded() {
        let nonce = "a".repeat(32);
        assert_eq!(
            database_name("native_test", &nonce).unwrap(),
            format!("lf_native_test_{nonce}")
        );
        assert_eq!(
            database_name("osdeploy_registration_test", &nonce)
                .unwrap()
                .len(),
            62
        );
        assert!(database_name("postgres", &nonce).is_err());
        assert!(database_name("native_test", "../foreign").is_err());
    }
    #[test]
    fn receipt_is_exact_canonical_and_duplicate_free() {
        assert!(receipt_response(GOOD.as_bytes()).is_ok());
        let duplicated = GOOD.replacen("{\"receipt\":", "{\"version\":1,\"receipt\":", 1);
        assert!(receipt_response(duplicated.as_bytes()).is_err());
        assert!(receipt_response(format!(" {GOOD}").as_bytes()).is_err());
    }

    #[test]
    fn receipt_rejects_each_schema_type_identity_and_encoding_violation() {
        let good: Value = serde_json::from_str(GOOD).unwrap();
        assert_eq!(
            receipt_response(format!("{GOOD}\n").as_bytes())
                .unwrap()
                .value(),
            good["receipt"]
        );
        for suffix in ["\n\n", " ", "\r\n"] {
            assert!(receipt_response(format!("{GOOD}{suffix}").as_bytes()).is_err());
        }
        for key in RECEIPT_KEYS {
            let mut v = good.clone();
            v["receipt"].as_object_mut().unwrap().remove(*key);
            assert!(
                receipt_response(v.to_string().as_bytes()).is_err(),
                "missing {key}"
            );
            for invalid in [Value::Null, json!([]), json!({}), json!(true)] {
                let mut v = good.clone();
                v["receipt"][*key] = invalid;
                assert!(
                    receipt_response(v.to_string().as_bytes()).is_err(),
                    "wrong type {key}"
                );
            }
        }
        for (key, invalid) in [
            ("version", json!(1.0)),
            ("version", json!(2)),
            ("session", json!("A".repeat(32))),
            ("pg_id", json!("b".repeat(63))),
            ("pg_image", json!("postgres:16-alpine")),
            ("runner_image", json!("sha256:foreign")),
            ("source_git_sha", json!("d".repeat(39))),
            ("marker", json!("lf_foreign")),
            ("netns", json!("net:[0]")),
            ("netns", json!("net:[01]")),
            ("system_identifier", json!("18446744073709551616")),
            ("system_identifier", json!("01")),
            ("system_identifier", json!("-1")),
        ] {
            let mut v = good.clone();
            v["receipt"][key] = invalid;
            assert!(
                receipt_response(v.to_string().as_bytes()).is_err(),
                "invalid {key}"
            );
        }
        let mut extra = good.clone();
        extra["receipt"]["endpoint"] = json!("127.0.0.1");
        assert!(receipt_response(extra.to_string().as_bytes()).is_err());
        assert!(receipt_response(&vec![b'x'; 8193]).is_err());
        assert!(receipt_response(GOOD.replace("receipt", "rec\\u0065ipt").as_bytes()).is_err());
        assert!(receipt_response(b"[]").is_err());
    }

    #[test]
    fn overrides_and_noncanonical_mode_never_select_a_backend() {
        for key in OVERRIDES {
            assert!(overrides_absent(|candidate| candidate == *key).is_err());
        }
        assert!(overrides_absent(|_| false).is_ok());
        for value in ["", "owned-v1 ", "OWNED-V1", "owned-v2"] {
            assert!(select_mode(true, Some(value)).is_err());
        }
        for family in [
            "native_proof",
            "native_test",
            "osdeploy_registration_test",
            "rust_controller_test",
        ] {
            assert!(database_name(family, &"f".repeat(32)).is_ok());
        }
        for nonce in [
            "A".repeat(32),
            "f".repeat(31),
            "f".repeat(33),
            "g".repeat(32),
        ] {
            assert!(database_name("native_test", &nonce).is_err());
        }
    }

    #[test]
    fn create_response_only_confirms_exact_identity_and_positive_oid() {
        let make = || {
            pending(
                receipt_response(GOOD.as_bytes()).unwrap(),
                "native_test",
                &"e".repeat(32),
            )
            .unwrap()
        };
        let mut database = make();
        let request = canonical(database.request().as_bytes()).unwrap();
        assert!(request["expected_oid"].is_null());
        assert_eq!(
            request["receipt"]["system_identifier"],
            "7682173388088000546"
        );
        let response = json!({"database":"lf_native_test_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "marker":"lf:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:native_test:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "oid":42,"version":1});
        for oid in [
            json!(0),
            json!(-1),
            json!(true),
            json!(42.0),
            json!(4294967296_u64),
            json!("42"),
            Value::Null,
        ] {
            let mut bad = response.clone();
            bad["oid"] = oid;
            assert!(database.accept_create(bad.to_string().as_bytes()).is_err());
            assert!(database.oid.is_none());
        }
        for key in ["database", "marker", "version"] {
            let mut bad = response.clone();
            bad[key] = json!("foreign");
            assert!(database.accept_create(bad.to_string().as_bytes()).is_err());
        }
        let mut extra = response.clone();
        extra["dsn"] = json!("foreign");
        assert!(
            database
                .accept_create(extra.to_string().as_bytes())
                .is_err()
        );
        assert!(database.accept_create(&vec![b'x'; 8193]).is_err());
        assert_eq!(
            database
                .accept_create(response.to_string().as_bytes())
                .unwrap(),
            "postgresql://postgres:local-linux-probe@127.0.0.1:5432/lf_native_test_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee?hostaddr=127.0.0.1"
        );
        assert_eq!(database.oid, Some(42));
        assert!(
            database
                .accept_create(response.to_string().as_bytes())
                .is_err()
        );
        assert_eq!(
            canonical(database.request().as_bytes()).unwrap()["expected_oid"],
            42
        );
    }

    #[test]
    fn cleanup_requires_exact_confirmed_absence_record() {
        let database = pending(
            receipt_response(GOOD.as_bytes()).unwrap(),
            "native_test",
            &"e".repeat(32),
        )
        .unwrap();
        let good = json!({"database":"lf_native_test_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "status":"absent", "version":1});
        assert!(database.accept_cleanup(good.to_string().as_bytes()).is_ok());
        for (key, bad) in [
            ("database", json!("foreign")),
            ("status", json!("dropped")),
            ("version", json!(true)),
            ("oid", json!(42)),
        ] {
            let mut value = good.clone();
            value[key] = bad;
            assert!(
                database
                    .accept_cleanup(value.to_string().as_bytes())
                    .is_err()
            );
        }
    }
}
