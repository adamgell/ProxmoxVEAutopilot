# PE package preparation provenance

Inspected source at `0e4a41e234401b4a383a653115018dc3f0740307`.

## Corrected source classification

`autopilot-proxmox/web/osdeploy_endpoints.py::pe_package` is the authenticated
`GET /pe/package/{run_id}` handler. It checks the run bearer, loads the run and
artifact from PostgreSQL, and calls `_package_response`. That response is a PE
deployment package. Its nested `agent` object describes the full-OS persistent
agent; the presence of that object does not make the entire response a full-OS
package. Earlier wording that implied otherwise was too broad.

## Actual field provenance

| Package field | Source | Meaning |
| --- | --- | --- |
| `run_id`, `workflow_name` | Loaded run row | Run correlation and workflow name; no workflow content digest |
| `identity.vmid`, `vm_uuid`, `mac`, `node` | Loaded run row | Server-held target declarations; not proof of a caller's VM identity |
| `identity.requested_name`, `pve_name`, `computer_name` | Run row plus legacy name fallback/normalization | Separate naming surfaces; none is a package label |
| `server_settings.role` | `run.server_role` | Server-selected role in the PE deployment package |
| `artifact` | Loaded artifact row plus `enrich_artifact` | Artifact metadata, including ISO/WIM hashes; these hashes do not identify the package response |
| `payloads.*.sha256` | Asset file hashing, or explicit null | Individual payload metadata; `osd_client.sha256` is null |
| `server_base_url` | `_base_url(request)` | Request-derived delivery URL; not immutable workflow provenance |
| `bearer_token`, `agent.bootstrap_token` | `_sign(run_id, ttl_seconds=...)` | Time-dependent run credentials with different intended lifetimes |
| `agent.bootstrap_url` | Base URL plus `/api/agent/v1/bootstrap` | Full-OS bootstrap endpoint, not a PE registration identity |
| `local_admin` | Run row/default | Secret-bearing configuration; must not appear in public preparation evidence |

There is no package-label field, whole-package digest, immutable package issuance
record, or exact workflow-content digest in this response. The handler builds it
on demand after closing its database context. Hashing a newly serialized response
would bind rotating credentials and secrets as well as request-derived URLs; it
would not reconstruct a stable package identity from legacy data.

## Concrete Rust integration

Implement preparation at the server's package materialization boundary. It must
receive a store-validated registered run and reconstructed `OsDeployPlanV1`, then
produce both the delivery package and a private immutable preparation record.
Use the registered plan fingerprint for workflow provenance, the resolved target
for VM identity, and the admitted role/naming fields for deployment semantics.
Do not call a public collection of validated strings a trusted preparation.

The preparation record should bind a server-generated package ID to canonical
non-secret semantic bytes and their computed digest. Version that encoding
explicitly. Keep delivery credentials and URLs in a separate delivery envelope;
credential aliases bind to the same preparation/session without changing its
identity. If exact delivered-byte attestation is required, retain a separate
restricted receipt with a distinct meaning instead of overloading the semantic
digest. A PE-specific callback URL must come from the configured callback profile,
not the nested full-OS bootstrap URL.

The Rust plan currently admits the native `base` role through its fixed native
contract. Preparation must preserve that supported scope until additional roles
are ported. A label may only use an explicitly documented plan naming field; do
not invent a new required label from the legacy response.

Required executable proofs at that boundary are: reconstruction from registered
plan; substitution rejection for run, VM, role, artifact and workflow; stable
semantic digest across credential renewal; digest change for any semantic field;
no secret leakage through debug/serialization/errors; and compile-fail tests for
public field construction and deserialization into the trusted preparation type.
No success-capable preparation type was added in this audit because neither
api-compat nor osdeploy-adapter currently owns that materialization boundary.

Validation: direct inspection of `_package_response`, `pe_package`,
`_asset_metadata`, `enrich_artifact`, `osdeploy_pg.get_run`, and Rust
`OsDeployPlanV1` construction/fingerprinting. This artifact makes no runtime or
compatibility-test claim.
