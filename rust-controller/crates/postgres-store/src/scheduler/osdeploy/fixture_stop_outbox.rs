//! Durable fixture stop intent. No return value is a physical dispatch permit.
use super::*;
use pve_port::fixture_support::{FixtureStopAdmissionReceiptV1, VersionedTestPowerSample};

/// Immutable result of consuming a selected fixture stop row. This is
/// bookkeeping only: it carries no release or physical-send authority.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FixtureStopOutboxConsumedV1 {
    pub operation_id: Uuid,
    pub attempt_id: Uuid,
    pub lease_token: Uuid,
    pub generation: i64,
    pub supervisor_generation: Uuid,
    pub admission_sha256: String,
    pub sample_sha256: String,
    pub provenance_sha256: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FixtureStopReleaseOutcomeStateV1 {
    Accepted,
    Refused,
    Ambiguous,
}

impl FixtureStopReleaseOutcomeStateV1 {
    fn as_str(self) -> &'static str {
        match self {
            Self::Accepted => "accepted",
            Self::Refused => "refused",
            Self::Ambiguous => "ambiguous",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FixtureStopReleaseOutcomeV1 {
    pub operation_id: Uuid,
    pub sequence: i64,
    pub state: FixtureStopReleaseOutcomeStateV1,
    pub receipt_sha256: Option<String>,
}

impl Scheduler {
    /// Validate the consumed envelope and proposal inside a locked transaction
    /// before any future fixture submit. This returns only a sequence-bearing
    /// preparation record and never releases or submits anything.
    pub async fn prepare_fixture_stop_release(
        &self,
        consumed: &FixtureStopOutboxConsumedV1,
        proposal: &pve_port::fixture_ipc::FixtureStopReleaseProposalV1,
        sequence: i64,
    ) -> Result<(), Error> {
        wire::require(sequence > 0)?;
        validate_consumed_proposal(consumed, proposal)?;
        let mut tx = self.store.pool().begin().await?;
        let row = sqlx::query("SELECT attempt_id,lease_token,generation,provenance_sha256,supervisor_generation FROM rust_controller.fixture_stop_outbox WHERE operation_id=$1")
            .bind(consumed.operation_id)
            .fetch_one(&mut *tx)
            .await?;
        wire::require(
            row.try_get::<Uuid, _>("attempt_id")? == consumed.attempt_id
                && row.try_get::<Uuid, _>("lease_token")? == consumed.lease_token
                && row.try_get::<i64, _>("generation")? == consumed.generation
                && row
                    .try_get::<Option<String>, _>("provenance_sha256")?
                    .as_deref()
                    == Some(consumed.provenance_sha256.as_str())
                && row.try_get::<Option<Uuid>, _>("supervisor_generation")?
                    == Some(consumed.supervisor_generation),
        )?;
        let marker: Option<Uuid> = sqlx::query_scalar("SELECT operation_id FROM rust_controller.fixture_stop_outbox_consumptions WHERE operation_id=$1")
            .bind(consumed.operation_id).fetch_optional(&mut *tx).await?;
        wire::require(marker == Some(consumed.operation_id))?;
        tx.rollback().await?;
        Ok(())
    }

    /// Persist one immutable fixture outcome after a successful preparation.
    /// This is bookkeeping only; it grants no release or submit capability.
    pub async fn record_fixture_stop_release_outcome(
        &self,
        consumed: &FixtureStopOutboxConsumedV1,
        proposal: &pve_port::fixture_ipc::FixtureStopReleaseProposalV1,
        sequence: i64,
        state: FixtureStopReleaseOutcomeStateV1,
        receipt: Option<serde_json::Value>,
    ) -> Result<FixtureStopReleaseOutcomeV1, Error> {
        self.prepare_fixture_stop_release(consumed, proposal, sequence)
            .await?;
        let receipt_sha256 = match (&state, &receipt) {
            (FixtureStopReleaseOutcomeStateV1::Accepted, Some(value)) => {
                Some(event_journal::payload_digest(value).map_err(|_| Error::Validation)?)
            }
            (FixtureStopReleaseOutcomeStateV1::Accepted, None)
            | (
                FixtureStopReleaseOutcomeStateV1::Refused
                | FixtureStopReleaseOutcomeStateV1::Ambiguous,
                Some(_),
            ) => {
                return Err(Error::Validation);
            }
            (_, None) => None,
        };
        let mut tx = self.store.pool().begin().await?;
        let existing = sqlx::query("SELECT submit_sequence,state,receipt_sha256 FROM rust_controller.fixture_stop_release_outcomes WHERE operation_id=$1")
            .bind(consumed.operation_id).fetch_optional(&mut *tx).await?;
        if let Some(row) = existing {
            wire::require(
                row.try_get::<i64, _>("submit_sequence")? == sequence
                    && row.try_get::<String, _>("state")? == state.as_str()
                    && row.try_get::<Option<String>, _>("receipt_sha256")? == receipt_sha256,
            )?;
            tx.rollback().await?;
            return Ok(FixtureStopReleaseOutcomeV1 {
                operation_id: consumed.operation_id,
                sequence,
                state,
                receipt_sha256,
            });
        }
        sqlx::query("INSERT INTO rust_controller.fixture_stop_release_outcomes(operation_id,attempt_id,lease_owner,generation,request_sha256,admission_sha256,sample_sha256,provenance_sha256,state,submit_sequence,receipt_json,receipt_sha256,recorded_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,clock_timestamp())")
            .bind(consumed.operation_id).bind(consumed.attempt_id).bind(consumed.lease_token)
            .bind(consumed.generation).bind(proposal.request_sha256()).bind(proposal.receipt_sha256())
            .bind(proposal.sample_sha256()).bind(&consumed.provenance_sha256).bind(state.as_str())
            .bind(sequence).bind(receipt).bind(&receipt_sha256).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(FixtureStopReleaseOutcomeV1 {
            operation_id: consumed.operation_id,
            sequence,
            state,
            receipt_sha256,
        })
    }

    /// Compatibility wrapper retaining the original one-use boolean API.
    pub async fn consume_fixture_stop_outbox(
        &self,
        grant: &LeaseGrant,
        receipt: &FixtureStopAdmissionReceiptV1,
    ) -> Result<bool, Error> {
        Ok(self
            .consume_fixture_stop_outbox_envelope(grant, receipt)
            .await?
            .is_some())
    }

    /// Select exact supervisor evidence or replay an identical selection. The
    /// caller must obtain the receipt from its trusted supervisor transport.
    /// Decoded input alone supplies no supervisor authenticity.
    pub async fn select_fixture_stop_outbox(
        &self,
        grant: &LeaseGrant,
        request: &pve_port::fixture_ipc::FixtureStageRequest,
        sample: &VersionedTestPowerSample,
        receipt: &FixtureStopAdmissionReceiptV1,
        provenance: &pve_port::fixture_ipc::FixtureSharedHistoryProvenanceV1,
    ) -> Result<(), Error> {
        let provenance_sha256 = provenance.sha256();
        wire::require(
            provenance_sha256.len() == 64
                && provenance_sha256.bytes().all(|b| {
                    b.is_ascii_digit() || (b'A'..=b'F').contains(&b) || (b'a'..=b'f').contains(&b)
                })
                && provenance_sha256.bytes().all(|b| !b.is_ascii_uppercase()),
        )?;
        sample
            .validate_admission_receipt(receipt)
            .map_err(|_| Error::Validation)?;
        sample
            .stop
            .validate_request(request)
            .map_err(|_| Error::Validation)?;
        self.revalidate_fixture_stop_authority(grant, &sample.authority)
            .await?;
        let admission = serde_json::to_string(receipt).map_err(|_| Error::Validation)?;
        let sample_json = serde_json::to_string(sample).map_err(|_| Error::Validation)?;
        let digest = receipt.sha256().map_err(|_| Error::Validation)?;
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        wire::require(
            snapshot.plan().stage() == OsDeployStage::PeEnsureStopped && !snapshot.cancelled(),
        )?;
        current_grant(&mut tx, self, grant, &snapshot).await?;
        wire::require(
            snapshot
                .dispatch()
                .ok_or(Error::CapabilityUnavailable)?
                .request()
                == request.request(),
        )?;
        let checked = now(&mut tx).await?;
        let millis = u64::try_from(checked.timestamp_millis()).map_err(|_| Error::Validation)?;
        wire::require(
            millis < sample.authority.lease_expires_unix_ms
                && millis < sample.authority.original_deadline_unix_ms,
        )?;
        let selected = sqlx::query("SELECT attempt_id,lease_token,generation,admission_sha256,admission_json,sample_json,provenance_sha256,supervisor_generation FROM rust_controller.fixture_stop_outbox WHERE operation_id=$1")
            .bind(grant.operation_id().as_uuid()).fetch_optional(&mut *tx).await?;
        if let Some(row) = selected {
            wire::require(
                row.try_get::<Uuid, _>("attempt_id")? == grant.attempt_id().as_uuid()
                    && row.try_get::<Uuid, _>("lease_token")? == grant.lease_token()
                    && row.try_get::<i64, _>("generation")? == grant.generation()
                    && row.try_get::<String, _>("admission_sha256")? == digest
                    && row.try_get::<String, _>("admission_json")? == admission
                    && row.try_get::<String, _>("sample_json")? == sample_json
                    && row
                        .try_get::<Option<String>, _>("provenance_sha256")?
                        .as_deref()
                        == Some(provenance_sha256.as_str())
                    && row.try_get::<Option<Uuid>, _>("supervisor_generation")?
                        == Some(provenance.generation()),
            )?;
        } else {
            sqlx::query("INSERT INTO rust_controller.fixture_stop_outbox(operation_id,attempt_id,lease_token,generation,admission_sha256,admission_json,sample_json,provenance_sha256,supervisor_generation,selected_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)")
                .bind(grant.operation_id().as_uuid()).bind(grant.attempt_id().as_uuid()).bind(grant.lease_token()).bind(grant.generation())
                .bind(digest).bind(admission).bind(sample_json).bind(&provenance_sha256).bind(provenance.generation()).bind(checked).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(())
    }

    /// Reserve exposure once. A lost commit acknowledgement is ambiguous:
    /// subsequent calls return false and may never authorize another send.
    /// Replacement leases cannot consume a prior owner's selection. Reconciliation
    /// remains required, and physical fixture stop release remains gated.
    pub async fn consume_fixture_stop_outbox_envelope(
        &self,
        grant: &LeaseGrant,
        receipt: &FixtureStopAdmissionReceiptV1,
    ) -> Result<Option<FixtureStopOutboxConsumedV1>, Error> {
        if !self.fixture_credential_delivery {
            return Err(Error::CapabilityUnavailable);
        }
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        wire::require(
            snapshot.plan().stage() == OsDeployStage::PeEnsureStopped && !snapshot.cancelled(),
        )?;
        current_grant(&mut tx, self, grant, &snapshot).await?;
        let row = sqlx::query("SELECT attempt_id,lease_token,generation,admission_sha256,admission_json,sample_json,provenance_sha256,supervisor_generation FROM rust_controller.fixture_stop_outbox WHERE operation_id=$1")
            .bind(grant.operation_id().as_uuid()).fetch_one(&mut *tx).await?;
        let selected: FixtureStopAdmissionReceiptV1 =
            serde_json::from_str(&row.try_get::<String, _>("admission_json")?)
                .map_err(|_| Error::Validation)?;
        let sample: VersionedTestPowerSample =
            serde_json::from_str(&row.try_get::<String, _>("sample_json")?)
                .map_err(|_| Error::Validation)?;
        sample
            .validate_admission_receipt(&selected)
            .map_err(|_| Error::Validation)?;
        wire::require(
            &selected == receipt
                && selected.sha256().map_err(|_| Error::Validation)?
                    == row.try_get::<String, _>("admission_sha256")?
                && row.try_get::<Uuid, _>("attempt_id")? == grant.attempt_id().as_uuid()
                && row.try_get::<Uuid, _>("lease_token")? == grant.lease_token()
                && row.try_get::<i64, _>("generation")? == grant.generation(),
        )?;
        let checked = now(&mut tx).await?;
        let millis = u64::try_from(checked.timestamp_millis()).map_err(|_| Error::Validation)?;
        wire::require(
            millis < sample.authority.lease_expires_unix_ms
                && millis < sample.authority.original_deadline_unix_ms,
        )?;
        let sample_value = serde_json::to_value(&sample).map_err(|_| Error::Validation)?;
        let sample_sha256 =
            event_journal::payload_digest(&sample_value).map_err(|_| Error::Validation)?;
        let provenance_sha256 = row
            .try_get::<Option<String>, _>("provenance_sha256")?
            .ok_or(Error::CapabilityUnavailable)?;
        let supervisor_generation = row
            .try_get::<Option<Uuid>, _>("supervisor_generation")?
            .ok_or(Error::CapabilityUnavailable)?;
        let result = sqlx::query("INSERT INTO rust_controller.fixture_stop_outbox_consumptions(operation_id,consumed_at) VALUES($1,$2) ON CONFLICT(operation_id) DO NOTHING")
            .bind(grant.operation_id().as_uuid()).bind(checked).execute(&mut *tx).await?;
        tx.commit().await?;
        if result.rows_affected() != 1 {
            return Ok(None);
        }
        Ok(Some(FixtureStopOutboxConsumedV1 {
            operation_id: grant.operation_id().as_uuid(),
            attempt_id: grant.attempt_id().as_uuid(),
            lease_token: grant.lease_token(),
            generation: grant.generation(),
            supervisor_generation,
            admission_sha256: row.try_get("admission_sha256")?,
            sample_sha256,
            provenance_sha256,
        }))
    }
}

fn validate_consumed_proposal(
    consumed: &FixtureStopOutboxConsumedV1,
    proposal: &pve_port::fixture_ipc::FixtureStopReleaseProposalV1,
) -> Result<(), Error> {
    wire::require(
        consumed.operation_id == proposal.operation()
            && consumed.attempt_id == proposal.attempt()
            && consumed.lease_token == proposal.lease_owner()
            && consumed.supervisor_generation == proposal.generation()
            && consumed.admission_sha256 == proposal.receipt_sha256()
            && consumed.sample_sha256 == proposal.sample_sha256()
            && consumed.provenance_sha256 == proposal.provenance_sha256(),
    )
}
