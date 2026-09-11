//! Fixture-only private delivery and one-time physical send exposure.
use super::*;
use api_compat::run_bearer::{IssuedRunBearer, RunBearerIdentity, issue_run_bearer};
use pve_port::ProvisioningMutationRequestV1;
#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};

#[derive(Clone, PartialEq, Eq)]
struct Binding {
    operation: Uuid,
    run: Uuid,
    attempt: Uuid,
    dispatch: Uuid,
    package: String,
    alias: Vec<u8>,
    expiry: i64,
    sink: Uuid,
}

/// Closed post-commit envelope. Only the concrete private sink can consume its
/// credential bytes; it cannot be serialized, cloned or converted to a permit.
/// ```compile_fail
/// let _: postgres_store::FixtureCredentialEnvelope = serde_json::from_str("{}").unwrap();
/// ```
pub struct FixtureCredentialEnvelope {
    binding: Binding,
    bearer: IssuedRunBearer,
}

/// Only successful durable acceptance by FixtureCredentialSink constructs this.
/// ```compile_fail
/// let _ = postgres_store::FixtureDeliveryAck {};
/// ```
pub struct FixtureDeliveryAck {
    binding: Binding,
}

pub enum FixtureDeliveryRecovery {
    Physical,
    NeedsDelivery(Box<FixtureCredentialEnvelope>),
    Acknowledged,
    Exposed,
}

/// A trusted local fixture spool, separate from journal, request and log storage.
/// The caller creates an owner-only directory and retains its stable sink UUID.
/// Exact-byte replay is durable and never replaces an existing credential.
pub struct FixtureCredentialSink {
    id: Uuid,
    directory: PathBuf,
}

impl FixtureCredentialSink {
    #[cfg(unix)]
    pub fn open(id: Uuid, directory: impl AsRef<Path>) -> Result<Self, Error> {
        let directory = directory.as_ref();
        let metadata = std::fs::symlink_metadata(directory).map_err(|_| Error::Validation)?;
        // Do not follow a final-component symlink or accept shared/private data
        // in a directory owned by a different effective user.
        if id.is_nil()
            || !directory.is_absolute()
            || !metadata.is_dir()
            || metadata.mode() & 0o777 != 0o700
            || metadata.uid() != unsafe { libc::geteuid() }
        {
            return Err(Error::Validation);
        }
        Ok(Self {
            id,
            directory: directory.to_path_buf(),
        })
    }
}

impl FixtureCredentialEnvelope {
    #[cfg(unix)]
    pub fn deliver(&self, sink: &FixtureCredentialSink) -> Result<FixtureDeliveryAck, Error> {
        wire::require(self.binding.sink == sink.id)?;
        // Validate the configured directory at every call, including replay.
        let _ = FixtureCredentialSink::open(sink.id, &sink.directory)?;
        let path = sink
            .directory
            .join(format!("{}.credential", self.binding.operation));
        let expected = format!(
            "{}\n{}\n{}\n{}\n{}\n{}\n{}\n{}\n",
            self.binding.run,
            self.binding.attempt,
            self.binding.dispatch,
            self.binding.package,
            self.binding.expiry,
            self.binding.sink,
            self.binding.operation,
            self.bearer.expose_for_delivery()
        );
        // Publish with a hard link after fsync: readers see either no final
        // name or the entire payload. A killed writer can leave an orphan temp
        // file but cannot publish a partial credential or generate an ack.
        let temp = sink.directory.join(format!(".{}.tmp", Uuid::now_v7()));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&temp)
            .map_err(|_| Error::Validation)?;
        let result = (|| {
            file.write_all(expected.as_bytes())
                .map_err(|_| Error::Validation)?;
            file.sync_all().map_err(|_| Error::Validation)?;
            match std::fs::hard_link(&temp, &path) {
                Ok(()) => (),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
                Err(_) => return Err(Error::Validation),
            }
            let mut accepted = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&path)
                .map_err(|_| Error::Validation)?;
            let metadata = accepted.metadata().map_err(|_| Error::Validation)?;
            wire::require(
                metadata.is_file()
                    && metadata.mode() & 0o777 == 0o600
                    && metadata.uid() == unsafe { libc::geteuid() }
                    && metadata.len() == expected.len() as u64,
            )?;
            let mut bytes = Vec::new();
            accepted
                .read_to_end(&mut bytes)
                .map_err(|_| Error::Validation)?;
            wire::require(bytes == expected.as_bytes())?;
            File::open(&sink.directory)
                .and_then(|d| d.sync_all())
                .map_err(|_| Error::Validation)?;
            Ok(FixtureDeliveryAck {
                binding: self.binding.clone(),
            })
        })();
        let _ = std::fs::remove_file(temp);
        result
    }
}

async fn binding(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
) -> Result<Binding, Error> {
    let row =
        sqlx::query("SELECT * FROM rust_controller.fixture_pe_deliveries WHERE operation_id=$1")
            .bind(operation.as_uuid())
            .fetch_one(&mut **tx)
            .await?;
    Ok(Binding {
        operation: row.try_get("operation_id")?,
        run: row.try_get("run_id")?,
        attempt: row.try_get("attempt_id")?,
        dispatch: row.try_get("dispatch_event_id")?,
        package: row.try_get("package_sha256")?,
        alias: row.try_get("alias_sha256")?,
        expiry: row.try_get("expires_at")?,
        sink: row.try_get("sink_id")?,
    })
}

async fn require_live(
    tx: &mut Transaction<'_, Postgres>,
    current: &LeaseGrant,
    b: &Binding,
) -> Result<(), Error> {
    let at = now(tx).await?;
    active_at(current, at)?;
    let deadline: DateTime<Utc> = sqlx::query_scalar("SELECT registration_deadline FROM rust_controller.fixture_pe_boot_sessions WHERE operation_id=$1")
        .bind(b.operation).fetch_one(&mut **tx).await?;
    if at >= deadline || at.timestamp() >= b.expiry {
        return Err(Error::FenceLost);
    }
    Ok(())
}

impl Scheduler {
    pub async fn arm_fixture_start_pe(
        &self,
        grant: &LeaseGrant,
        revision: i64,
        event: EventId,
        request: &ProvisioningMutationRequestV1,
        secret: &[u8],
    ) -> Result<FixtureCredentialEnvelope, Error> {
        if !self.fixture_credential_delivery {
            return Err(Error::CapabilityUnavailable);
        }
        let mut tx = self.store.pool().begin().await?;
        let (_dispatch, dispatch_event) = self
            .prepare_dispatch_in_tx(&mut tx, grant, revision, event, request, true)
            .await?;
        let row = sqlx::query("SELECT s.*,o.credential_sink_id,o.source_namespace,o.claim_kind,o.claim_value FROM rust_controller.fixture_pe_boot_sessions s JOIN rust_controller.fixture_osdeploy_origins o USING(run_id) WHERE s.operation_id=$1")
            .bind(grant.operation_id().as_uuid()).fetch_one(&mut *tx).await?;
        let run: Uuid = row.try_get("run_id")?;
        wire::require(
            row.try_get::<String, _>("source_namespace")? == "rust-owned-fixture-v1"
                && row.try_get::<String, _>("claim_kind")? == "text"
                && row.try_get::<String, _>("claim_value")? == run.to_string(),
        )?;
        let deadline: DateTime<Utc> = row.try_get("registration_deadline")?;
        let expiry = deadline.timestamp();
        let bearer = issue_run_bearer(RunBearerIdentity::Text(&run.to_string()), expiry, secret)
            .map_err(|_| Error::Validation)?;
        let b = Binding {
            operation: grant.operation_id().as_uuid(),
            run,
            attempt: grant.attempt_id().as_uuid(),
            dispatch: dispatch_event.as_uuid(),
            package: row.try_get("package_sha256")?,
            alias: bearer.metadata().alias_sha256().to_vec(),
            expiry,
            sink: row.try_get("credential_sink_id")?,
        };
        let at = now(&mut tx).await?;
        fixture_credential::retain_alias_owner(
            &mut tx,
            bearer.metadata().alias_sha256(),
            fixture_credential::FixtureAliasOwner {
                operation: b.operation,
                run: b.run,
                attempt: b.attempt,
                package_sha256: &b.package,
                expires_at: b.expiry,
            },
            at,
        )
        .await?;
        sqlx::query("INSERT INTO rust_controller.fixture_pe_deliveries(operation_id,run_id,attempt_id,dispatch_event_id,package_sha256,alias_sha256,expires_at,sink_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8)")
            .bind(b.operation).bind(b.run).bind(b.attempt).bind(b.dispatch).bind(&b.package).bind(&b.alias).bind(b.expiry).bind(b.sink).execute(&mut *tx).await?;
        let snapshot = load::load_execution(&mut tx, grant.operation_id()).await?;
        let (current, _) = current_grant(&mut tx, self, grant, &snapshot).await?;
        require_live(&mut tx, &current, &b).await?;
        tx.commit().await?;
        Ok(FixtureCredentialEnvelope { binding: b, bearer })
    }

    pub async fn recover_fixture_start_pe(
        &self,
        grant: &LeaseGrant,
        secret: &[u8],
    ) -> Result<FixtureDeliveryRecovery, Error> {
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        if !requires_delivery(&mut tx, &snapshot).await? {
            tx.commit().await?;
            return Ok(FixtureDeliveryRecovery::Physical);
        }
        let (current, _) = current_grant(&mut tx, self, grant, &snapshot).await?;
        admit(self, &snapshot, snapshot.plan().workflow_sha256())?;
        if snapshot.dispatch().is_none() {
            return Err(Error::Validation);
        }
        let b = binding(&mut tx, grant.operation_id()).await?;
        let exposed: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.fixture_pe_delivery_exposures WHERE operation_id=$1)").bind(b.operation).fetch_one(&mut *tx).await?;
        if exposed {
            tx.commit().await?;
            return Ok(FixtureDeliveryRecovery::Exposed);
        }
        require_live(&mut tx, &current, &b).await?;
        let bearer = issue_run_bearer(
            RunBearerIdentity::Text(&b.run.to_string()),
            b.expiry,
            secret,
        )
        .map_err(|_| Error::Validation)?;
        wire::require(bearer.metadata().alias_sha256().as_slice() == b.alias)?;
        let acked: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.fixture_pe_delivery_acks WHERE operation_id=$1)").bind(b.operation).fetch_one(&mut *tx).await?;
        tx.commit().await?;
        Ok(if acked {
            FixtureDeliveryRecovery::Acknowledged
        } else {
            FixtureDeliveryRecovery::NeedsDelivery(Box::new(FixtureCredentialEnvelope {
                binding: b,
                bearer,
            }))
        })
    }

    pub async fn record_fixture_delivery_ack(
        &self,
        grant: &LeaseGrant,
        ack: &FixtureDeliveryAck,
    ) -> Result<(), Error> {
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        admit(self, &snapshot, snapshot.plan().workflow_sha256())?;
        let (current, _) = current_grant(&mut tx, self, grant, &snapshot).await?;
        let b = binding(&mut tx, grant.operation_id()).await?;
        wire::require(b == ack.binding)?;
        require_live(&mut tx, &current, &b).await?;
        sqlx::query("INSERT INTO rust_controller.fixture_pe_delivery_acks(operation_id,acknowledged_at) VALUES($1,clock_timestamp()) ON CONFLICT(operation_id) DO NOTHING")
            .bind(b.operation).execute(&mut *tx).await?;
        require_live(&mut tx, &current, &b).await?;
        tx.commit().await?;
        Ok(())
    }

    pub async fn expose_fixture_start_pe_once(
        &self,
        grant: &LeaseGrant,
    ) -> Result<Option<(OsDeployDispatchPermit, OsDeployResponseCapture)>, Error> {
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        admit(self, &snapshot, snapshot.plan().workflow_sha256())?;
        let (current, epoch) = current_grant(&mut tx, self, grant, &snapshot).await?;
        let b = binding(&mut tx, grant.operation_id()).await?;
        let exposed: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.fixture_pe_delivery_exposures WHERE operation_id=$1)").bind(b.operation).fetch_one(&mut *tx).await?;
        if exposed {
            tx.commit().await?;
            return Ok(None);
        }
        require_live(&mut tx, &current, &b).await?;
        let acked: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.fixture_pe_delivery_acks WHERE operation_id=$1)").bind(b.operation).fetch_one(&mut *tx).await?;
        if !acked {
            return Err(Error::CapabilityUnavailable);
        }
        sqlx::query("INSERT INTO rust_controller.fixture_pe_delivery_exposures(operation_id,lease_acquisition_event_id,generation,worker_id,exposed_at) VALUES($1,$2,$3,$4,clock_timestamp())")
            .bind(b.operation).bind(epoch.as_uuid()).bind(self.generation).bind(&self.worker_id).execute(&mut *tx).await?;
        require_live(&mut tx, &current, &b).await?;
        let dispatch = snapshot.dispatch().ok_or(Error::Validation)?.clone();
        tx.commit().await?;
        Ok(Some(receipt::committed(dispatch, load::id(b.dispatch)?)))
    }
}
