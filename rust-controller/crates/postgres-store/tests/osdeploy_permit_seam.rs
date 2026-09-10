//! Compile-check both existing concrete callers and the sealed object seam.
//! Runtime dispatch/receipt behavior remains covered by OSDeploy durability tests.
use postgres_store::OsDeployDispatchPermit;
use pve_port::{MutationReceipt, NativeFakePve, ProvisioningFakePort, PveWriteError};

async fn concrete(
    permit: OsDeployDispatchPermit,
    fake: &NativeFakePve,
) -> Result<MutationReceipt, PveWriteError> {
    permit.submit_fake_once(fake).await
}

async fn sealed_object(
    permit: OsDeployDispatchPermit,
    fake: &dyn ProvisioningFakePort,
) -> Result<MutationReceipt, PveWriteError> {
    permit.submit_fake_once(fake).await
}

#[test]
fn concrete_and_sealed_object_callers_typecheck() {
    // Referencing these functions keeps their bodies type-checked without
    // manufacturing a permit outside the successful database transaction.
    let _ = concrete;
    let _ = sealed_object;
}
