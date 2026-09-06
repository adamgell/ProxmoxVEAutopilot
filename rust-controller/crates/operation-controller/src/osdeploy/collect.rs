//! Physical observations keep their actual errors and original evidence fence.
use super::{OsDeployControllerError as Error, READ_BOUND};
use chrono::Utc;
use pve_port::*;
use std::{collections::BTreeSet, future::Future};
use tokio::time::{Instant, timeout_at};

async fn read<T>(
    deadline: Instant,
    future: impl Future<Output = Result<T, PveReadError>>,
) -> NativeRead<T> {
    let result = if Instant::now() >= deadline {
        Err(PveReadError::TimedOut)
    } else {
        timeout_at(deadline.min(Instant::now() + READ_BOUND), future)
            .await
            .unwrap_or(Err(PveReadError::TimedOut))
    };
    NativeRead::new(Utc::now(), result)
}

pub(super) async fn collect(
    fake: &NativeFakePve,
    context: &ProvisioningEvaluationContextV1,
    deadline: Instant,
) -> Result<ProvisioningEvidenceV1, Error> {
    Box::pin(async move {
        let expected = context.facts().plan.expected();
        let vm = expected.vm();
        let node = vm.node();
        let inventory = read(deadline, fake.cluster_vms()).await;
        let mut identities = Vec::new();
        let mut coverage = ProvisioningCoverageV1::Partial;
        if let Ok(snapshot) = &inventory.result {
            if snapshot.vms().len() <= 32 {
                coverage = ProvisioningCoverageV1::Complete;
            }
            for entry in snapshot.vms().values().take(32) {
                identities.push(
                    ProvisioningIdentityReadV1::new(
                        entry.node().clone(),
                        entry.vmid(),
                        read(
                            deadline,
                            fake.provisioning_identity(entry.node(), entry.vmid()),
                        )
                        .await,
                    )
                    .map_err(|_| Error::Validation)?,
                );
            }
        }
        let storages: BTreeSet<_> = [expected.deployment_iso_volid(), expected.driver_iso_volid()]
            .into_iter()
            .map(|value| {
                value
                    .split_once(':')
                    .map(|pair| pair.0)
                    .ok_or(Error::Validation)
            })
            .collect::<Result<_, _>>()?;
        let mut media = Vec::new();
        for storage in storages {
            let storage = StorageName::parse(storage).map_err(|_| Error::Validation)?;
            media.push(read(deadline, fake.provisioning_media(node, &storage)).await);
        }
        let receipt = match &context.facts().dispatch {
            ProvisioningDispatchStateV1::Recorded { receipt, .. } => receipt.clone(),
            _ => None,
        };
        let task = if let Some(MutationReceipt::Task(upid)) =
            receipt.as_ref().map(ProvisioningReceiptV1::receipt)
        {
            Some(read(deadline, fake.task_status(node, upid)).await)
        } else {
            None
        };
        ProvisioningEvidenceV1::new(ProvisioningEvidenceInputV1 {
            binding: context.facts().binding.clone(),
            plan: context.facts().plan.clone(),
            source: NativeEvidenceSource::FakePve,
            node: Some(read(deadline, fake.node_status(node)).await),
            storage: Some(read(deadline, fake.storage_status(node, vm.storage())).await),
            bridges: Some(read(deadline, fake.bridges(node)).await),
            inventory: Some(inventory),
            inventory_coverage: coverage,
            identities,
            source_config: Some(
                read(
                    deadline,
                    fake.provisioning_vm_config(node, vm.source_vmid()),
                )
                .await,
            ),
            source_power: Some(read(deadline, fake.vm_status(node, vm.source_vmid())).await),
            target_config: Some(
                read(
                    deadline,
                    fake.provisioning_vm_config(node, vm.target_vmid()),
                )
                .await,
            ),
            target_power: Some(read(deadline, fake.vm_status(node, vm.target_vmid())).await),
            media,
            qga: None,
            task,
            receipt,
            collected_at: Utc::now(),
        })
        .map_err(|_| Error::Validation)
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn expired_endpoint_does_not_poll_a_physical_read() {
        let polled = std::cell::Cell::new(false);
        let value = read(Instant::now(), async {
            polled.set(true);
            Ok::<_, PveReadError>(())
        })
        .await;
        assert!(!polled.get());
        assert!(matches!(value.result, Err(PveReadError::TimedOut)));
    }
}
