use super::*;

/// Semantic binding derived from a validated registered plan. This does not
/// attest delivered bytes, verified artifacts, session issuance or dispatch authority.
/// Credentials and delivery URLs are deliberately absent from this identity.
///
/// ```compile_fail,E0451
/// use postgres_store::RegisteredPePackageSemanticsV1;
/// fn forge(existing: RegisteredPePackageSemanticsV1) -> RegisteredPePackageSemanticsV1 {
///     RegisteredPePackageSemanticsV1 { semantic_sha256: String::new(), ..existing }
/// }
/// ```
/// ```compile_fail,E0277
/// let _: postgres_store::RegisteredPePackageSemanticsV1 = serde_json::from_str("{}").unwrap();
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct RegisteredPePackageSemanticsV1 {
    run_id: RunId,
    start_pe_operation_id: OperationId,
    workflow_sha256: String,
    semantic_sha256: String,
}

impl OsDeployRegistrationV1 {
    /// Binds the entire admitted non-secret plan through its canonical fingerprint.
    /// No caller-supplied replacement run, role, artifact or VM can enter here.
    pub fn pe_package_semantics(
        &self,
    ) -> Result<RegisteredPePackageSemanticsV1, OsDeployStoreError> {
        let workflow_sha256 = self
            .plan
            .fingerprint()
            .map_err(|_| OsDeployStoreError::Validation)?;
        if workflow_sha256 != self.ids.workflow_sha256 {
            return Err(OsDeployStoreError::Validation);
        }
        let run_id = self.ids.run_id;
        let start_pe_operation_id = self.ids.operation(OsDeployStage::StartPe);
        let semantic_sha256 = event_journal::payload_digest(&serde_json::json!({
            "schema": "registered_pe_package_semantics_v1",
            "run_id": run_id,
            "start_pe_operation_id": start_pe_operation_id,
            "workflow_sha256": workflow_sha256,
        }))
        .map_err(|_| OsDeployStoreError::Validation)?;
        Ok(RegisteredPePackageSemanticsV1 {
            run_id,
            start_pe_operation_id,
            workflow_sha256,
            semantic_sha256,
        })
    }
}

impl RegisteredPePackageSemanticsV1 {
    pub const fn run_id(&self) -> RunId {
        self.run_id
    }
    pub const fn start_pe_operation_id(&self) -> OperationId {
        self.start_pe_operation_id
    }
    pub fn workflow_sha256(&self) -> &str {
        &self.workflow_sha256
    }
    pub fn semantic_sha256(&self) -> &str {
        &self.semantic_sha256
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn registration() -> OsDeployRegistrationV1 {
        let plan = osdeploy_adapter::restore_osdeploy_plan_v1(
            include_str!("../../../osdeploy-adapter/tests/fixtures/plan-v1.json"),
            "47035f6731b8a27dceabbe9fb2ff63aa952204dfa3b6420d94a9a35c1fe3e5a9",
        )
        .unwrap();
        OsDeployRegistrationV1 {
            ids: OsDeployWorkflowIds {
                run_id: RunId::new(),
                operations: std::array::from_fn(|_| OperationId::new()),
                workflow_sha256: plan.fingerprint().unwrap(),
            },
            stages: stage::plans(&plan).unwrap(),
            plan,
        }
    }
    #[test]
    fn semantic_identity_binds_run_operation_and_complete_plan() {
        let original = registration();
        let expected = original.pe_package_semantics().unwrap();
        assert_eq!(expected, original.clone().pe_package_semantics().unwrap());
        let mut changed = original.clone();
        changed.ids.run_id = RunId::new();
        assert_ne!(
            expected.semantic_sha256(),
            changed.pe_package_semantics().unwrap().semantic_sha256()
        );
        changed = original.clone();
        changed.ids.operations[ordinal(OsDeployStage::StartPe)] = OperationId::new();
        assert_ne!(
            expected.semantic_sha256(),
            changed.pe_package_semantics().unwrap().semantic_sha256()
        );
        for (field, replacement) in [
            ("/os_edition", "Professional"),
            ("/image_name", "Other image"),
            ("/system_serial", "other-serial"),
            ("/vm/node", "node-b"),
            ("/artifact/build_label", "20260906010203"),
        ] {
            let mut json = serde_json::to_value(&original.plan).unwrap();
            *json.pointer_mut(field).unwrap() = replacement.into();
            let digest = event_journal::payload_digest(&json).unwrap();
            changed = original.clone();
            changed.plan =
                osdeploy_adapter::restore_osdeploy_plan_v1(&json.to_string(), &digest).unwrap();
            assert_eq!(
                changed.pe_package_semantics(),
                Err(OsDeployStoreError::Validation)
            );
            changed.ids.workflow_sha256 = digest;
            assert_ne!(
                expected.semantic_sha256(),
                changed.pe_package_semantics().unwrap().semantic_sha256()
            );
        }
    }
    #[test]
    fn unsupported_role_cannot_be_reconstructed_into_preparation() {
        let original = registration();
        let mut json = serde_json::to_value(original.plan()).unwrap();
        json["server_role"] = "domain-controller".into();
        let digest = event_journal::payload_digest(&json).unwrap();
        assert!(osdeploy_adapter::restore_osdeploy_plan_v1(&json.to_string(), &digest).is_err());
    }
    #[test]
    fn public_receipt_contains_only_identity_and_digests() {
        let identity = registration().pe_package_semantics().unwrap();
        let value = serde_json::to_value(&identity).unwrap();
        let keys: Vec<_> = value
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            [
                "run_id",
                "semantic_sha256",
                "start_pe_operation_id",
                "workflow_sha256"
            ]
        );
        assert_eq!(identity.semantic_sha256().len(), 64);
    }
}
