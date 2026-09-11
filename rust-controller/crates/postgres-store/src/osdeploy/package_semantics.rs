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

/// Canonical semantic package bytes materialized from a registered plan.
/// This is an internal semantic envelope, not the legacy delivery wire format.
/// It contains profile references, never resolved credentials or delivery URLs.
/// It grants no session, issuance, callback or dispatch authority.
///
/// ```compile_fail,E0277
/// let _: postgres_store::MaterializedPePackageSemanticsV1 = serde_json::from_str("{}").unwrap();
/// ```
/// ```compile_fail,E0451
/// use postgres_store::MaterializedPePackageSemanticsV1;
/// fn forge(existing: MaterializedPePackageSemanticsV1) -> MaterializedPePackageSemanticsV1 {
///     MaterializedPePackageSemanticsV1 { canonical_bytes: vec![], ..existing }
/// }
/// ```
#[derive(Clone, Eq, PartialEq)]
pub struct MaterializedPePackageSemanticsV1 {
    identity: RegisteredPePackageSemanticsV1,
    canonical_bytes: Vec<u8>,
    envelope_sha256: String,
}

impl std::fmt::Debug for MaterializedPePackageSemanticsV1 {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MaterializedPePackageSemanticsV1")
            .field("identity", &self.identity)
            .field("envelope_sha256", &self.envelope_sha256)
            .finish_non_exhaustive()
    }
}

impl MaterializedPePackageSemanticsV1 {
    pub fn identity(&self) -> &RegisteredPePackageSemanticsV1 {
        &self.identity
    }
    pub fn canonical_bytes(&self) -> &[u8] {
        &self.canonical_bytes
    }
    pub fn envelope_sha256(&self) -> &str {
        &self.envelope_sha256
    }
}

impl OsDeployRegistrationV1 {
    /// Materializes the native fixture completion protocol definition together
    /// with its registered package. This is an opt-in schema; existing v1
    /// delivery digests remain unchanged. It does not accept a completion report.
    #[cfg(feature = "fixture-ipc")]
    pub fn materialize_fixture_completion_package(
        &self,
    ) -> Result<MaterializedPePackageSemanticsV1, OsDeployStoreError> {
        let identity = self.pe_package_semantics()?;
        let requirement = serde_json::json!({
            "schema": "fixture_pe_completion_requirement_v1",
            "package_semantic_sha256": identity.semantic_sha256(),
            "completion_operation_id": self.ids.operation(OsDeployStage::PeComplete),
            "milestones": [{
                "id": "boot-files-staged.v1",
                "required": true,
                "result_schema": "fixture_boot_files_staged_result_v1",
                "required_fields": ["image_applied", "boot_files_staged", "boot_files_verified"],
                "field_type": "boolean",
                "success": "all_required_fields_true",
                "failure": "any_required_field_false",
                "unknown_fields": "reject",
                "missing_fields": "reject",
            }],
        });
        let definition_sha256 = event_journal::payload_digest(&requirement)
            .map_err(|_| OsDeployStoreError::Validation)?;
        let envelope = serde_json::json!({
            "schema": "materialized_fixture_pe_completion_package_v1",
            "identity": identity,
            "plan": self.plan,
            "completion_requirement": requirement,
            "completion_definition_sha256": definition_sha256,
        });
        Ok(MaterializedPePackageSemanticsV1 {
            identity,
            canonical_bytes: event_journal::canonical_json_bytes(&envelope)
                .map_err(|_| OsDeployStoreError::Validation)?,
            envelope_sha256: event_journal::payload_digest(&envelope)
                .map_err(|_| OsDeployStoreError::Validation)?,
        })
    }

    /// Materializes immutable semantic bytes without accepting replacement fields.
    /// The complete admitted plan preserves VM, role, artifact and profile provenance.
    pub fn materialize_pe_package_semantics(
        &self,
    ) -> Result<MaterializedPePackageSemanticsV1, OsDeployStoreError> {
        let identity = self.pe_package_semantics()?;
        let envelope = serde_json::json!({
            "schema": "materialized_pe_package_semantics_v1",
            "identity": identity,
            "plan": self.plan,
        });
        let canonical_bytes = event_journal::canonical_json_bytes(&envelope)
            .map_err(|_| OsDeployStoreError::Validation)?;
        let envelope_sha256 =
            event_journal::payload_digest(&envelope).map_err(|_| OsDeployStoreError::Validation)?;
        Ok(MaterializedPePackageSemanticsV1 {
            identity,
            canonical_bytes,
            envelope_sha256,
        })
    }
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
    #[cfg(feature = "fixture-ipc")]
    #[test]
    fn fixture_completion_definition_is_package_bound_and_explicit() {
        let registered = registration();
        let legacy = registered.materialize_pe_package_semantics().unwrap();
        let package = registered.materialize_fixture_completion_package().unwrap();
        assert_eq!(
            package,
            registered
                .clone()
                .materialize_fixture_completion_package()
                .unwrap()
        );
        let value: serde_json::Value = serde_json::from_slice(package.canonical_bytes()).unwrap();
        let requirement = &value["completion_requirement"];
        assert_eq!(
            value["schema"],
            "materialized_fixture_pe_completion_package_v1"
        );
        assert_eq!(
            requirement["package_semantic_sha256"],
            package.identity().semantic_sha256()
        );
        assert_eq!(
            requirement["completion_operation_id"],
            serde_json::to_value(registered.ids.operation(OsDeployStage::PeComplete)).unwrap()
        );
        assert_eq!(
            requirement["milestones"],
            serde_json::json!([{
                "id": "boot-files-staged.v1", "required": true,
                "result_schema": "fixture_boot_files_staged_result_v1",
                "required_fields": ["image_applied", "boot_files_staged", "boot_files_verified"],
                "field_type": "boolean", "success": "all_required_fields_true",
                "failure": "any_required_field_false", "unknown_fields": "reject", "missing_fields": "reject"
            }])
        );
        assert_eq!(
            value["completion_definition_sha256"],
            event_journal::payload_digest(requirement).unwrap()
        );
        assert_eq!(
            package.envelope_sha256(),
            event_journal::payload_digest(&value).unwrap()
        );
        assert_ne!(package.envelope_sha256(), legacy.envelope_sha256());
        assert_eq!(
            legacy,
            registered.materialize_pe_package_semantics().unwrap()
        );
        for stage in [OsDeployStage::StartPe, OsDeployStage::PeComplete] {
            let mut changed = registered.clone();
            changed.ids.operations[ordinal(stage)] = OperationId::new();
            let changed = changed.materialize_fixture_completion_package().unwrap();
            let changed: serde_json::Value =
                serde_json::from_slice(changed.canonical_bytes()).unwrap();
            assert_ne!(
                value["completion_definition_sha256"],
                changed["completion_definition_sha256"]
            );
        }
        let mut changed = registered.clone();
        changed.ids.workflow_sha256 = "0".repeat(64);
        assert_eq!(
            changed.materialize_fixture_completion_package(),
            Err(OsDeployStoreError::Validation)
        );
    }
    #[test]
    fn semantic_identity_binds_run_operation_and_complete_plan() {
        let original = registration();
        let expected = original.pe_package_semantics().unwrap();
        let materialized = original.materialize_pe_package_semantics().unwrap();
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
            assert_eq!(
                changed.materialize_pe_package_semantics(),
                Err(OsDeployStoreError::Validation)
            );
            changed.ids.workflow_sha256 = digest;
            assert_ne!(
                expected.semantic_sha256(),
                changed.pe_package_semantics().unwrap().semantic_sha256()
            );
            assert_ne!(
                materialized.envelope_sha256(),
                changed
                    .materialize_pe_package_semantics()
                    .unwrap()
                    .envelope_sha256()
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

    #[test]
    fn materialization_preserves_registered_plan_and_canonical_bytes() {
        let registered = registration();
        let package = registered.materialize_pe_package_semantics().unwrap();
        assert_eq!(
            package,
            registered
                .clone()
                .materialize_pe_package_semantics()
                .unwrap()
        );
        let envelope: serde_json::Value =
            serde_json::from_slice(package.canonical_bytes()).unwrap();
        assert_eq!(envelope["schema"], "materialized_pe_package_semantics_v1");
        assert_eq!(
            envelope["plan"],
            serde_json::to_value(registered.plan()).unwrap()
        );
        assert_eq!(
            envelope["identity"],
            serde_json::to_value(package.identity()).unwrap()
        );
        assert_eq!(
            event_journal::canonical_json_bytes(&envelope).unwrap(),
            package.canonical_bytes()
        );
        assert_eq!(
            event_journal::payload_digest(&envelope).unwrap(),
            package.envelope_sha256()
        );
        let debug = format!("{package:?}");
        assert!(!debug.contains("system_serial"));
        assert!(!debug.contains("secret_profile_id"));
        assert!(!debug.contains("canonical_bytes"));
        for field in [
            "bearer_token",
            "local_admin",
            "bootstrap_token",
            "server_base_url",
        ] {
            assert!(envelope["plan"].get(field).is_none());
        }
        let mut substituted = registered.clone();
        substituted.ids.workflow_sha256 = "0".repeat(64);
        assert_eq!(
            substituted.materialize_pe_package_semantics(),
            Err(OsDeployStoreError::Validation)
        );
        substituted = registered;
        substituted.ids.run_id = RunId::new();
        assert_ne!(
            package.envelope_sha256(),
            substituted
                .materialize_pe_package_semantics()
                .unwrap()
                .envelope_sha256()
        );
    }
}
