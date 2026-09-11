use super::*;
use serde::{Deserializer, de};
pub(in crate::provisioning) fn object<'de, D: Deserializer<'de>, T: Deserialize<'de>>(
    d: D,
) -> Result<T, D::Error> {
    struct Object<T>(std::marker::PhantomData<T>);
    impl<'de, T: Deserialize<'de>> de::Visitor<'de> for Object<T> {
        type Value = T;
        fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("provisioning object")
        }
        fn visit_map<A: de::MapAccess<'de>>(self, a: A) -> Result<T, A::Error> {
            T::deserialize(de::value::MapAccessDeserializer::new(a))
        }
    }
    d.deserialize_map(Object(std::marker::PhantomData))
        .map_err(|_| de::Error::custom(InvalidProvisioning))
}
pub(in crate::provisioning) fn required_nullable<'de, D: Deserializer<'de>, T: Deserialize<'de>>(
    d: D,
) -> Result<Option<T>, D::Error> {
    Option::<T>::deserialize(d)
}
pub(in crate::provisioning) fn source_wire<'de, D: Deserializer<'de>>(
    d: D,
) -> Result<NativeEvidenceSource, D::Error> {
    match String::deserialize(d)?.as_str() {
        "fake_pve" => Ok(NativeEvidenceSource::FakePve),
        "pve_api" => Ok(NativeEvidenceSource::PveApi),
        _ => Err(de::Error::custom(InvalidProvisioning)),
    }
}

// Existing native types retain their old decoder contracts. At the new rich
// boundary require their exact serialized shape, rejecting duplicate keys at
// every nested level before converting, without changing any legacy API.
struct UniqueValue(serde_json::Value);
pub(in crate::provisioning) fn unique<'de, D: Deserializer<'de>>(
    d: D,
) -> Result<serde_json::Value, D::Error> {
    UniqueValue::deserialize(d).map(|v| v.0)
}
impl<'de> Deserialize<'de> for UniqueValue {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> de::Visitor<'de> for V {
            type Value = UniqueValue;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("canonical value")
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Self::Value, E> {
                Ok(UniqueValue(v.into()))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Self::Value, E> {
                Ok(UniqueValue(v.into()))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Self::Value, E> {
                Ok(UniqueValue(v.into()))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(UniqueValue(v.into()))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Self::Value, E> {
                Ok(UniqueValue(v.into()))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(UniqueValue(serde_json::Value::Null))
            }
            fn visit_none<E: de::Error>(self) -> Result<Self::Value, E> {
                self.visit_unit()
            }
            fn visit_seq<A: de::SeqAccess<'de>>(self, mut a: A) -> Result<Self::Value, A::Error> {
                let mut v = Vec::new();
                while let Some(UniqueValue(x)) = a.next_element()? {
                    v.push(x);
                }
                Ok(UniqueValue(v.into()))
            }
            fn visit_map<A: de::MapAccess<'de>>(self, mut a: A) -> Result<Self::Value, A::Error> {
                let mut v = serde_json::Map::new();
                while let Some((k, UniqueValue(x))) = a.next_entry::<String, UniqueValue>()? {
                    if v.insert(k, x).is_some() {
                        return Err(de::Error::custom(InvalidProvisioning));
                    }
                }
                Ok(UniqueValue(v.into()))
            }
        }
        d.deserialize_any(V)
    }
}
pub(in crate::provisioning) fn canonical<
    'de,
    D: Deserializer<'de>,
    T: de::DeserializeOwned + Serialize,
>(
    d: D,
) -> Result<T, D::Error> {
    let UniqueValue(v) =
        UniqueValue::deserialize(d).map_err(|_| de::Error::custom(InvalidProvisioning))?;
    let decoded: T =
        serde_json::from_value(v.clone()).map_err(|_| de::Error::custom(InvalidProvisioning))?;
    if serde_json::to_value(&decoded).map_err(|_| de::Error::custom(InvalidProvisioning))? != v {
        return Err(de::Error::custom(InvalidProvisioning));
    }
    Ok(decoded)
}
pub(in crate::provisioning) fn nullable_canonical<
    'de,
    D: Deserializer<'de>,
    T: de::DeserializeOwned + Serialize,
>(
    d: D,
) -> Result<Option<T>, D::Error> {
    canonical(d)
}

impl<'de> Deserialize<'de> for ProvisioningBindingV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct W {
            contract_version: u16,
            run_id: RunId,
            operation_id: OperationId,
            attempt_id: AttemptId,
            workflow_sha256: String,
            operation_plan_sha256: String,
            evidence_fence: u64,
        }
        let w: W = object(d)?;
        if w.contract_version != 1
            || !hash_valid(&w.workflow_sha256)
            || !hash_valid(&w.operation_plan_sha256)
        {
            return Err(de::Error::custom(InvalidProvisioning));
        }
        Ok(Self {
            contract_version: 1,
            run_id: w.run_id,
            operation_id: w.operation_id,
            attempt_id: w.attempt_id,
            workflow_sha256: w.workflow_sha256.to_ascii_lowercase(),
            operation_plan_sha256: w.operation_plan_sha256.to_ascii_lowercase(),
            evidence_fence: w.evidence_fence,
        })
    }
}
impl<'de> Deserialize<'de> for ProvisioningDispatchV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct W {
            request: ProvisioningMutationRequestV1,
            #[serde(deserialize_with = "source_wire")]
            source: NativeEvidenceSource,
            preflight_event_id: EventId,
            original_generation: i64,
            dispatch_revision: u64,
            dispatched_at: DateTime<Utc>,
            request_sha256: String,
        }
        let w: W = object(d)?;
        let v = Self::new(ProvisioningDispatchInputV1 {
            request: w.request,
            source: w.source,
            preflight_event_id: w.preflight_event_id,
            original_generation: w.original_generation,
            dispatch_revision: w.dispatch_revision,
            dispatched_at: w.dispatched_at,
        })
        .map_err(de::Error::custom)?;
        if v.request_sha256 != w.request_sha256 {
            return Err(de::Error::custom(InvalidProvisioning));
        }
        Ok(v)
    }
}
impl<'de> Deserialize<'de> for ProvisioningReceiptV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct W {
            dispatch: ProvisioningDispatchV1,
            accepted_at: DateTime<Utc>,
            #[serde(deserialize_with = "canonical")]
            receipt: MutationReceipt,
        }
        let w: W = object(d)?;
        Self::new(w.dispatch, w.accepted_at, w.receipt).map_err(de::Error::custom)
    }
}
impl<'de> Deserialize<'de> for ProvisioningIdentitySnapshotV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct W {
            contract_version: u16,
            node: NodeName,
            vmid: Vmid,
            #[serde(deserialize_with = "source_wire")]
            source: NativeEvidenceSource,
            name: NativeVmName,
            is_template: bool,
            config_digest: String,
            uuid: VmUuid,
            mac: MacAddress,
            primary_storage: StorageName,
            primary_volume: String,
            coverage: ProvisioningCoverageV1,
            observed_at: DateTime<Utc>,
        }
        let w: W = object(d)?;
        if w.contract_version != 1
            || !super::super::requests::state::safe_atom(&w.config_digest, 256)
            || !super::super::requests::state::safe_volume(&w.primary_volume)
        {
            return Err(de::Error::custom(InvalidProvisioning));
        }
        Ok(Self {
            contract_version: 1,
            node: w.node,
            vmid: w.vmid,
            source: w.source,
            name: w.name,
            is_template: w.is_template,
            config_digest: w.config_digest,
            uuid: w.uuid,
            mac: w.mac,
            primary_storage: w.primary_storage,
            primary_volume: w.primary_volume,
            coverage: w.coverage,
            observed_at: w.observed_at,
        })
    }
}
impl<'de> Deserialize<'de> for ProvisioningIdentityReadV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct W {
            node: NodeName,
            vmid: Vmid,
            #[serde(deserialize_with = "canonical")]
            read: NativeRead<ProvisioningIdentitySnapshotV1>,
        }
        let w: W = object(d)?;
        Self::new(w.node, w.vmid, w.read).map_err(de::Error::custom)
    }
}
impl<'de> Deserialize<'de> for ProvisioningEvidenceInputV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct W {
            binding: ProvisioningBindingV1,
            plan: ProvisioningOperationPlanV1,
            #[serde(deserialize_with = "source_wire")]
            source: NativeEvidenceSource,
            collected_at: DateTime<Utc>,
            #[serde(deserialize_with = "nullable_canonical")]
            node: Option<NativeRead<NodeStatus>>,
            #[serde(deserialize_with = "nullable_canonical")]
            storage: Option<NativeRead<StorageStatus>>,
            #[serde(deserialize_with = "nullable_canonical")]
            bridges: Option<NativeRead<BridgeInventory>>,
            #[serde(deserialize_with = "nullable_canonical")]
            inventory: Option<NativeRead<ClusterVmInventory>>,
            inventory_coverage: ProvisioningCoverageV1,
            identities: Vec<ProvisioningIdentityReadV1>,
            #[serde(deserialize_with = "nullable_canonical")]
            source_config: Option<NativeRead<ProvisioningVmConfigV1>>,
            #[serde(deserialize_with = "nullable_canonical")]
            source_power: Option<NativeRead<VmPowerStatus>>,
            #[serde(deserialize_with = "nullable_canonical")]
            target_config: Option<NativeRead<ProvisioningVmConfigV1>>,
            #[serde(deserialize_with = "nullable_canonical")]
            target_power: Option<NativeRead<VmPowerStatus>>,
            #[serde(deserialize_with = "canonical")]
            media: Vec<NativeRead<ProvisioningMediaInventoryV1>>,
            #[serde(deserialize_with = "nullable_canonical")]
            qga: Option<NativeRead<ProvisioningQgaObservationV1>>,
            #[serde(deserialize_with = "nullable_canonical")]
            task: Option<NativeRead<TaskStatus>>,
            #[serde(deserialize_with = "required_nullable")]
            receipt: Option<ProvisioningReceiptV1>,
        }
        let w: W = object(d)?;
        let i = Self {
            binding: w.binding,
            plan: w.plan,
            source: w.source,
            collected_at: w.collected_at,
            node: w.node,
            storage: w.storage,
            bridges: w.bridges,
            inventory: w.inventory,
            inventory_coverage: w.inventory_coverage,
            identities: w.identities,
            source_config: w.source_config,
            source_power: w.source_power,
            target_config: w.target_config,
            target_power: w.target_power,
            media: w.media,
            qga: w.qga,
            task: w.task,
            receipt: w.receipt,
        };
        validation::validate(&i).map_err(de::Error::custom)?;
        Ok(i)
    }
}
impl<'de> Deserialize<'de> for ProvisioningEvidenceV1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        Self::new(ProvisioningEvidenceInputV1::deserialize(d)?).map_err(de::Error::custom)
    }
}
