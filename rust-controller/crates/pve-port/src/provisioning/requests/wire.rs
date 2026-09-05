use super::super::evidence::wire::{canonical, object, unique};
use super::*;
use serde::{Deserializer, de};
impl<'de> Deserialize<'de> for ProvisioningBeforeStateV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct W {
            #[serde(deserialize_with = "canonical")]
            config: ProvisioningVmConfigV1,
            #[serde(deserialize_with = "canonical")]
            power: VmPowerStatus,
        }
        let w: W = object(d)?;
        Self::new(w.config, w.power).map_err(de::Error::custom)
    }
}
impl<'de> Deserialize<'de> for CloneProvisioningRequestV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct W {
            contract_version: u16,
            binding: ProvisioningBindingV1,
            #[serde(deserialize_with = "canonical")]
            plan: ProvisioningOperationPlanV1,
            #[serde(deserialize_with = "canonical")]
            clone: CloneRequest,
            expected_before: ProvisioningBeforeStateV1,
        }
        let w: W = object(d)?;
        let r = Self {
            contract_version: w.contract_version,
            binding: w.binding,
            plan: w.plan,
            clone: w.clone,
            expected_before: w.expected_before,
        };
        r.validate().map_err(de::Error::custom)?;
        Ok(r)
    }
}
impl<'de> Deserialize<'de> for OwnedRequest {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct W {
            contract_version: u16,
            binding: ProvisioningBindingV1,
            #[serde(deserialize_with = "canonical")]
            plan: ProvisioningOperationPlanV1,
            #[serde(deserialize_with = "canonical")]
            clone_request: CloneRequest,
            clone_binding: ProvisioningBindingV1,
            clone_evidence_sha256: String,
            #[serde(deserialize_with = "canonical")]
            clone_config: ProvisioningVmConfigV1,
            predecessor_binding: ProvisioningBindingV1,
            #[serde(deserialize_with = "canonical")]
            predecessor_plan: ProvisioningOperationPlanV1,
            predecessor_evidence_sha256: String,
            #[serde(deserialize_with = "canonical")]
            predecessor_config: ProvisioningVmConfigV1,
            #[serde(deserialize_with = "canonical")]
            predecessor_power: VmPowerStatus,
            expected_before: ProvisioningBeforeStateV1,
        }
        let w: W = object(d)?;
        let r = Self {
            contract_version: w.contract_version,
            binding: w.binding,
            plan: w.plan,
            clone_request: w.clone_request,
            clone_binding: w.clone_binding,
            clone_evidence_sha256: w.clone_evidence_sha256,
            clone_config: w.clone_config,
            predecessor_binding: w.predecessor_binding,
            predecessor_plan: w.predecessor_plan,
            predecessor_evidence_sha256: w.predecessor_evidence_sha256,
            predecessor_config: w.predecessor_config,
            predecessor_power: w.predecessor_power,
            expected_before: w.expected_before,
        };
        r.validate().map_err(de::Error::custom)?;
        Ok(r)
    }
}
macro_rules! decode_owned {($ty:ident,[$($action:pat_param)|+])=>{
    impl<'de> Deserialize<'de> for $ty {
        fn deserialize<D:Deserializer<'de>>(d:D)->Result<Self,D::Error> {let r=OwnedRequest::deserialize(d)?;if !matches!(r.plan.action(),$($action)|+) {return Err(de::Error::custom(InvalidProvisioning));} Ok(Self(r))}
    }
}}
decode_owned!(GrowDiskRequestV1, [ProvisioningActionV1::EnsureCapacity]);
decode_owned!(
    ConfigureProvisioningRequestV1,
    [ProvisioningActionV1::ConfigurePe | ProvisioningActionV1::ConfigureDisk]
);
decode_owned!(
    StartProvisioningRequestV1,
    [ProvisioningActionV1::StartPe | ProvisioningActionV1::StartDisk]
);
decode_owned!(
    StopProvisioningRequestV1,
    [ProvisioningActionV1::EnsureStopped]
);
impl<'de> Deserialize<'de> for ProvisioningMutationRequestV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        // Unique nested keys must be checked before the heterogeneous request is
        // buffered for dispatch by its string-only discriminator.
        #[derive(Serialize, Deserialize)]
        #[serde(deny_unknown_fields)]
        struct W {
            kind: String,
            #[serde(deserialize_with = "unique")]
            request: serde_json::Value,
        }
        let w: W = object(d)?;
        macro_rules! decode {
            ($ty:ty,$variant:ident) => {
                serde_json::from_value::<$ty>(w.request)
                    .map(Self::$variant)
                    .map_err(|_| de::Error::custom(InvalidProvisioning))
            };
        }
        match w.kind.as_str() {
            "clone" => decode!(CloneProvisioningRequestV1, Clone),
            "grow_disk" => decode!(GrowDiskRequestV1, GrowDisk),
            "configure" => decode!(ConfigureProvisioningRequestV1, Configure),
            "start" => decode!(StartProvisioningRequestV1, Start),
            "stop" => decode!(StopProvisioningRequestV1, Stop),
            _ => Err(de::Error::custom(InvalidProvisioning)),
        }
    }
}
