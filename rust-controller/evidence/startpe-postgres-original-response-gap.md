# PostgreSQL to fixture StartPe restoration: original response gap

Source inspected at `8d29ae1d6f4d96ef87f02c162f14248da88884e8`.

The store already has a typed observation API:
`load_osdeploy_operation(operation).receipt()` returns the validated
`ProvisioningReceiptV1` from a closed operation snapshot. The execution loader
checks its journal event, operation/attempt, original dispatch event, request
digest, original capture time, receipt kind and UPID. That API grants no new
dispatch permit. Adding another read-only wrapper would not supply the missing
StartPe fixture receipt.

The missing data is at the original response capture boundary:

| Boundary | Retained data |
| --- | --- |
| Fixture daemon response | Versioned fixture envelope, fixture UUID, request digest, submission sequence, mutation receipt |
| `LateStartContext::submit` | Envelope bytes stored only in this process's mutex; caller receives `MutationReceipt` |
| `OsDeployDispatchPermit::submit_fake_once` | Returns only `MutationReceipt` |
| `record_osdeploy_pve_receipt` | Journal and SQL persist task/synchronous kind, UPID and first server capture time |
| Store receipt reload | Reconstructs `ProvisioningReceiptV1` from the above semantic fields |
| `with_late_start_receipt` | Requires the original fixture envelope bytes in a predecessor-bound adapter |

The SQL schema `0005_osdeploy_durability.sql` has no original fixture envelope,
submission sequence, fixture UUID or original fixture supervisor owner/generation
columns. The scheduler's bigint generation is a different identity domain from
the fixture supervisor UUID generation. They cannot be substituted.

Therefore a PostgreSQL getter cannot reconstruct the required response bytes
from the current stored values. Inferring a sequence from UPID text or encoding
a new receipt with matching JSON claims would invent original-response evidence.
The adapter's verified respelled-JSON test at `3a3b70d0` already demonstrates that
the daemon rejects different bytes even when the parsed claims match.

## Next implementation boundary

Introduce a closed fixture response-capture value created at successful typed
IPC submission, retaining exact response envelope bytes plus the original
fixture stage identity and predecessor binding. It must not be constructible
from callback JSON or from a generic `MutationReceipt`. Keep the ordinary
production-facing mutation trait unchanged unless a broader reviewed contract
requires changing it.

The existing original-dispatch capture transaction should optionally persist
this fixture value atomically with its semantic receipt and journal record.
Validate the exact dispatch/request/attempt and all fixture identity fields;
store immutable bytes and digest. A storage retry must preserve the first
capture time and original bytes; conflicting bytes must fail. The loader can
then return a closed observation-only fixture restoration record after matching
the semantic receipt, journal and source bytes. It must not restore a send permit.

Required proofs are late SQL failure rollback, same-response replay, conflicting
bytes refusal, wrong run/operation/attempt/fixture-owner refusal, and process
restart readback with checkpoint/send still unavailable. A lost IPC response
requires separately validated lookup of the daemon's original accepted journal
effect; absence must remain unknown and must not trigger a resend.

This audit used the store capture implementation, receipt loader, SQL schema,
closed snapshot accessors and StartPe adapter submission path. No new runtime
behavior or tests are claimed. The missing capture/storage contract prevents a
truthful retrieval-only implementation; generic evaluator mapping remains an
independent work item. No production mutation or physical stop occurred.
