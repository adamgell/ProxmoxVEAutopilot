# PowerShell Delivery Plane

## Purpose

Provide real-time, auditable delivery and control of approved PowerShell work
to enrolled Windows endpoints. The Autopilot Agent remains the execution
authority; the controller never stores endpoint administrator credentials or
opens an unrestricted remote shell.

## Decision

Use the existing Agent/controller work channel as the delivery transport.
PowerShell runs locally under the Agent on the selected endpoint. Remote
PowerShell is an optional future relay capability for legacy machines that
cannot run the Agent; it is not the default transport and is never exposed as
a generic interactive command endpoint.

## V1 architecture

1. An operator creates a typed operation with an approved package identifier,
   fixed target contract, expected SHA-256, and non-secret parameters.
2. The controller stores the immutable package and metadata in an
   agent-authenticated artifact store. Package download requires the receiving
   Agent token; public file URLs are not used for private packages.
3. The targeted Agent receives the operation through its existing work poll,
   acknowledges it, downloads the package, verifies its SHA-256 and manifest,
   and runs PowerShell 7 locally.
4. The Agent emits bounded structured progress, sanitized stdout/stderr,
   evidence paths, exit status, and reboot-required state.
5. The controller records the state transition and exposes it through typed
   endpoints for live status and safe operator retry.

## Initial operation: publish Setup-CM module

`publish_setup_cm_module` targets only `agent-labz1-dc02` and accepts only a
controller artifact that contains the committed Setup-CM runtime archive.
After hash and required-entry verification, the Agent atomically replaces:

- `C:\SetupCm\Modules\setup-cm.zip`
- `C:\SetupCm\Modules\setup-cm.manifest.json`

The destination, archive names, required script entries, and manifest schema
are fixed in the Agent. The operation cannot select an arbitrary path,
PowerShell command, or UNC location. This immediately resolves the current
stale archive/manifest mismatch before a client-install retry.

## Package contract

Every package has:

- a stable package identifier and content SHA-256;
- source commit when the source is Git-backed;
- a manifest listing the permitted operation type and required runtime files;
- a maximum size and a controller-generated download URL;
- no secrets, credentials, keys, media, or tenant configuration.

The Agent rejects unknown operation types, mismatched hashes, missing required
archive entries, oversized payloads, and any parameter outside its typed
schema.

## Remote PowerShell policy

The Agent's local PowerShell 7 host is the standard endpoint executor. A later
relay operation may use PowerShell remoting only when all of the following are
true:

- the target cannot host an Agent;
- the relay is a designated, enrolled endpoint;
- the target set and command package are fixed and approved;
- credentials come from the relay's private runtime store, never Git or the
  controller work request;
- the evidence identifies both relay and remote target.

Interactive remoting, free-form commands, and controller-held credentials are
out of scope.

## Realtime control and recovery

Operations use `queued`, `acknowledged`, `running`, `progress`,
`awaiting_reboot`, `complete`, and `failed` states. A bounded operation lease
prevents duplicate work; a restarted Agent resumes only after it validates the
package hash and its prior evidence state. Retries create a new auditable work
item rather than overwriting prior evidence.

## Verification

- Unit tests cover controller validation, authenticated artifact access, Agent
  hash/manifest/destination checks, and state transitions.
- Contract tests reject arbitrary paths, URLs, scripts, parameters, and
  unauthorized Agent/package combinations.
- Integration proof publishes the current Setup-CM module on DC02, checks the
  archive and manifest hashes agree, and reruns the typed MECM client install
  to a successful result.

## Non-goals

This version does not build an interactive terminal, a general script runner,
a secret store, broad endpoint targeting, or MECM/Intune transport adapters.
Those can be layered onto the same package and operation contracts after the
fixed DC02 publication flow is proven.
