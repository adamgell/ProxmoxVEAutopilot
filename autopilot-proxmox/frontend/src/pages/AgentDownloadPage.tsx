import { useCallback, useEffect, useMemo, useState } from "react";
import { Copy, Download } from "lucide-react";

import { fetchJson, postJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Panel } from "../components/ui";
import type {
  AgentDownloadBootstrapTokenResponse,
  AppBootstrap,
  LabBubbleInfrastructureNode,
  VmsFleetResponse
} from "../contracts";
import { fallbackText, vmDisplayName } from "../viewModels";

interface BuildHostStatus {
  readonly vmid: string | number | null;
  readonly name: string;
  readonly node: string | null;
  readonly expected_agent_id: string;
  readonly expected_computer_name: string;
  readonly agent_ready: boolean;
  readonly agent_state: string;
  readonly last_heartbeat_at: string | null;
  readonly last_heartbeat_age_seconds: number | null;
  readonly agent_version: string | null;
  readonly primary_ipv4: string | null;
}

interface BuildHostResponse {
  readonly schema_version: number;
  readonly build_host: BuildHostStatus;
  readonly artifacts?: {
    readonly ready?: boolean;
    readonly agent_msi_ready?: boolean;
    readonly iso_ready?: boolean;
  };
  readonly media?: {
    readonly windows_iso_volid?: string | null;
    readonly virtio_iso_volid?: string | null;
  };
}

interface AgentDownloadPageProps {
  readonly bootstrap: AppBootstrap;
}

interface ControllerCandidate {
  readonly id: string;
  readonly label: string;
  readonly node: LabBubbleInfrastructureNode;
}

const emptyFleet: VmsFleetResponse = {
  vms: [],
  proxmox_vms: [],
  missing_vms: [],
  agents: [],
  autopilot_devices: [],
  bubble_topology: {
    workstation_fleets: [],
    critical_infrastructure: [],
    connected_services: [],
    unassigned_assets: [],
    warnings: [],
    gate_states: []
  },
  ap_error: "",
  cache_refreshing: false,
  generated_at: ""
};

function trimControllerUrl(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) {
    return window.location.origin;
  }
  const withScheme = /^https?:\/\//iu.test(trimmed) ? trimmed : `http://${trimmed}`;
  return withScheme.replace(/\/+$/u, "");
}

function controllerLabel(node: LabBubbleInfrastructureNode): string {
  const name = node.vm ? vmDisplayName(node.vm) : fallbackText(node.asset.agent_id || node.role);
  const vmid = node.asset.vmid ?? node.vm?.vmid;
  return `${node.bubble.name} / ${name}${typeof vmid === "number" ? ` / VM ${String(vmid)}` : ""}`;
}

function candidateSort(left: LabBubbleInfrastructureNode, right: LabBubbleInfrastructureNode): number {
  const leftDc = left.role === "domain_controller" ? 0 : 1;
  const rightDc = right.role === "domain_controller" ? 0 : 1;
  if (leftDc !== rightDc) {
    return leftDc - rightDc;
  }
  return controllerLabel(left).localeCompare(controllerLabel(right), undefined, { sensitivity: "base" });
}

function controllerCandidates(fleet: VmsFleetResponse): readonly ControllerCandidate[] {
  return [...(fleet.bubble_topology?.critical_infrastructure ?? [])]
    .sort(candidateSort)
    .map((node) => ({
      id: node.asset.id,
      label: controllerLabel(node),
      node
    }));
}

function psDoubleQuoted(value: string): string {
  return value.replace(/`/gu, "``").replace(/"/gu, '`"');
}

function buildInstallCommand(
  node: LabBubbleInfrastructureNode | undefined,
  controllerUrl: string,
  msiUrl: string,
  postinstallUrl: string,
  bootstrapToken: string
): string {
  const vmid = node?.asset.vmid ?? node?.vm?.vmid;
  const agentId = node?.asset.agent_id || node?.agent?.agent_id || (typeof vmid === "number" ? `agent-vm-${String(vmid)}` : "agent-critical-infra");
  const vmidArgument = typeof vmid === "number" ? ` -Vmid ${String(vmid)}` : "";
  const token = bootstrapToken || "<bootstrap-token-unavailable>";
  return [
    `$ControllerUrl = "${psDoubleQuoted(controllerUrl)}"`,
    '$MsiPath = Join-Path $env:TEMP "AutopilotAgent.msi"',
    '$PostInstall = Join-Path $env:TEMP "autopilotagent-postinstall.ps1"',
    `Invoke-WebRequest -UseBasicParsing -Uri "${psDoubleQuoted(msiUrl)}" -OutFile $MsiPath`,
    `Invoke-WebRequest -UseBasicParsing -Uri "${psDoubleQuoted(postinstallUrl)}" -OutFile $PostInstall`,
    'Start-Process msiexec.exe -Wait -ArgumentList "/i `"$MsiPath`" /qn /norestart"',
    `& $PostInstall -ServerUrl "${psDoubleQuoted(controllerUrl)}" -BootstrapToken "${psDoubleQuoted(token)}" -AgentId "${psDoubleQuoted(agentId)}"${vmidArgument} -Phase "critical_infra"`
  ].join("\n");
}

async function copyText(value: string): Promise<void> {
  await navigator.clipboard.writeText(value);
}

function buildHostStateLabel(status: BuildHostStatus | null): string {
  if (!status) {
    return "Loading...";
  }
  if (!status.vmid) {
    return "Not provisioned";
  }
  const heartbeatFresh =
    status.last_heartbeat_age_seconds !== null &&
    status.last_heartbeat_age_seconds <= 300;
  if (status.agent_ready) {
    return "Ready";
  }
  // Treat a recent heartbeat as effectively ready even when the backend
  // sets agent_ready=false (e.g. device row present without a telemetry
  // row, or expected computer-name match still syncing).
  if (heartbeatFresh) {
    return "Ready";
  }
  if (status.agent_state === "missing") {
    return "VM exists, agent missing";
  }
  if (status.agent_state === "stale") {
    return "Agent stale";
  }
  if (status.agent_state === "registered") {
    return "Registered, waiting for heartbeat";
  }
  return status.agent_state || "Unknown";
}

function formatAge(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) {
    return "never";
  }
  if (seconds < 60) {
    return `${String(Math.round(seconds))}s ago`;
  }
  if (seconds < 3600) {
    return `${String(Math.round(seconds / 60))}m ago`;
  }
  if (seconds < 86400) {
    return `${String(Math.round(seconds / 3600))}h ago`;
  }
  return `${String(Math.round(seconds / 86400))}d ago`;
}

function BuildHostPanel() {
  const [status, setStatus] = useState<BuildHostStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [actionResult, setActionResult] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await fetchJson<BuildHostResponse>("/api/setup/v1/build-host");
      setStatus(data.build_host);
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to load build host status");
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    const initialTimer = window.setTimeout(() => { void load(); }, 0);
    const timer = window.setInterval(() => { void load(); }, 30000);
    return () => {
      window.clearTimeout(initialTimer);
      window.clearInterval(timer);
    };
  }, [load]);

  const provision = useCallback(async () => {
    setBusy(true);
    setActionResult("");
    setError("");
    try {
      const result = await postJson<{ readonly vmid?: number; readonly name?: string; readonly node?: string }>(
        "/api/setup/v1/build-host/vm",
        {}
      );
      const vmid = result.vmid ?? "?";
      const name = result.name ?? "build host";
      setActionResult(`Provisioned ${name} as VM ${String(vmid)}. The VM is booting; the agent will register once Windows Setup completes.`);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to provision build host");
    } finally {
      setBusy(false);
    }
  }, [load]);

  const repairAgent = useCallback(async () => {
    setBusy(true);
    setActionResult("");
    setError("");
    try {
      await postJson<Record<string, unknown>>("/api/setup/v1/build-host/repair-agent", {});
      setActionResult("AutopilotAgent install/repair completed on the build host. Heartbeat should arrive within a minute.");
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to install AutopilotAgent on the build host");
    } finally {
      setBusy(false);
    }
  }, [load]);

  const stateLabel = buildHostStateLabel(status);
  const vmidLabel = status?.vmid ? String(status.vmid) : "-";
  const heartbeat = status?.last_heartbeat_age_seconds ?? null;
  // Treat the agent as healthy if either the backend says agent_ready,
  // OR we have a recent heartbeat (within 5 min). The backend's
  // agent_ready is stricter than a fresh heartbeat -- it also requires
  // the row to come from agent_telemetry and the vmid/computer name to
  // match an expectation, which can leave a recently-checked-in device
  // looking "registered but unready" until those sync up.
  const heartbeatFresh = heartbeat !== null && heartbeat <= 300;
  const agentHealthy = Boolean(status?.agent_ready) || heartbeatFresh;

  return (
    <Panel title="Build host">
      {!loaded ? (
        <p className="empty">Checking build host status...</p>
      ) : (
        <>
          <dl className="fleet-detail-grid">
            <div><dt>Status</dt><dd>{stateLabel}</dd></div>
            <div><dt>VMID</dt><dd>{vmidLabel}</dd></div>
            <div><dt>Node</dt><dd>{status?.node ?? "-"}</dd></div>
            <div><dt>Agent ID</dt><dd>{status?.expected_agent_id || "-"}</dd></div>
            <div><dt>Agent version</dt><dd>{status?.agent_version ?? "-"}</dd></div>
            <div><dt>Last heartbeat</dt><dd>{formatAge(heartbeat)}</dd></div>
          </dl>
          {error ? <p className="notice" role="status">{error}</p> : null}
          {actionResult ? <p className="notice" role="status">{actionResult}</p> : null}
          {!status?.vmid ? (
            <div className="build-host-actions">
              <p className="build-host-hint">
                No build host VM is provisioned on the cluster yet. Click below to create
                an unattended Windows Server VM with the AutopilotAgent pre-seeded; it
                will register as <code>{status?.expected_agent_id || "buildhost-<vmid>"}</code> once Windows
                Setup completes.
              </p>
              <button
                type="button"
                className="utility-button"
                onClick={() => { void provision(); }}
                disabled={busy}
              >
                {busy ? "Provisioning..." : "Build build host"}
              </button>
            </div>
          ) : agentHealthy ? null : (
            <div className="build-host-actions">
              <p className="build-host-hint">
                Build host VM <code>{String(status.vmid)}</code> exists on
                {" "}<code>{status.node ?? "?"}</code> but the AutopilotAgent is{" "}
                <strong>{status.agent_state || "missing"}</strong>. Install or repair the
                agent over QEMU Guest Agent so the build host can pick up MSI / ISO /
                WIM build jobs.
              </p>
              <button
                type="button"
                className="utility-button"
                onClick={() => { void repairAgent(); }}
                disabled={busy}
              >
                {busy ? "Installing..." : "Install AutopilotAgent on build host"}
              </button>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}

function CopyButton({ value, label }: { readonly value: string; readonly label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="fleet-action fleet-action--command"
      onClick={() => {
        void copyText(value).then(() => {
          setCopied(true);
          window.setTimeout(() => { setCopied(false); }, 1600);
        });
      }}
    >
      <Copy size={14} aria-hidden="true" />
      <span>{copied ? "Copied" : label}</span>
    </button>
  );
}

export function AgentDownloadPage({ bootstrap }: AgentDownloadPageProps) {
  const [fleet, setFleet] = useState<VmsFleetResponse>(emptyFleet);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [controllerUrl, setControllerUrl] = useState(window.location.origin);
  const [bootstrapToken, setBootstrapToken] = useState("");

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetchJson<VmsFleetResponse>("/api/vms/fleet"),
      fetchJson<AgentDownloadBootstrapTokenResponse>("/api/react/agent-download/bootstrap-token")
    ])
      .then(([payload, tokenPayload]) => {
        if (!cancelled) {
          setFleet(payload);
          setBootstrapToken(tokenPayload.bootstrap_token);
          const first = controllerCandidates(payload).at(0);
          if (first !== undefined) {
            setSelectedId(first.id);
          }
          // Do NOT overwrite controllerUrl with an infra node's address;
          // the controller is this Flask server, not any target VM.
        }
      })
      .catch((exc: unknown) => {
        if (!cancelled) {
          setError(exc instanceof Error ? exc.message : "Unable to load VM topology");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const candidates = useMemo(() => controllerCandidates(fleet), [fleet]);

  const selected = candidates.find((candidate) => candidate.id === selectedId) ?? candidates[0];
  const normalizedControllerUrl = trimControllerUrl(controllerUrl);
  const msiUrl = `${normalizedControllerUrl}/api/cloudosd/assets/autopilotagent.msi`;
  const postinstallUrl = `${normalizedControllerUrl}/api/cloudosd/assets/autopilotagent-postinstall.ps1`;
  const seedExeUrl = `${normalizedControllerUrl}/api/setup/v1/agent-seed/win-x64/AutopilotAgent.exe`;
  const installCommand = buildInstallCommand(selected?.node, normalizedControllerUrl, msiUrl, postinstallUrl, bootstrapToken);

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="AutopilotAgent Download"
      section="Fleet"
      path="/react/agent-download"
      action={<a className="action-link" href={msiUrl}><Download size={14} aria-hidden="true" />Download MSI</a>}
    >
      {loading ? <div className="progress" aria-label="Loading agent download topology"><span /></div> : null}
      {error ? <p className="notice" role="status">{error}</p> : null}

      <section className="agent-download-grid" aria-label="Agent download builder">
        <Panel title="Install target">
          <div className="bubble-form">
            <div className="bubble-form-grid">
              <label className="bubble-form-field">
                <span>Install target VM</span>
                <select
                  aria-label="Install target VM"
                  value={selected?.id ?? ""}
                  onChange={(event) => {
                    setSelectedId(event.target.value);
                  }}
                >
                  {candidates.length ? candidates.map((candidate) => (
                    <option key={candidate.id} value={candidate.id}>{candidate.label}</option>
                  )) : <option value="">No critical infrastructure nodes</option>}
                </select>
              </label>
              <label className="bubble-form-field">
                <span>Autopilot controller URL</span>
                <input
                  aria-label="Autopilot controller URL"
                  value={controllerUrl}
                  onChange={(event) => { setControllerUrl(event.target.value); }}
                  placeholder="https://autopilot.example"
                />
                <small className="bubble-form-help">
                  Where the agent reports back to (this autopilot Flask
                  server). Defaults to the URL you opened this page on;
                  override only if installing on a host that resolves it
                  differently.
                </small>
              </label>
            </div>
            {selected ? (
              <dl className="fleet-detail-grid">
                <div><dt>Bubble</dt><dd>{selected.node.bubble.name}</dd></div>
                <div><dt>Role</dt><dd>{selected.node.role.replaceAll("_", " ")}</dd></div>
                <div><dt>VM</dt><dd>{selected.node.vm ? vmDisplayName(selected.node.vm) : fallbackText(selected.node.asset.vmid)}</dd></div>
                <div><dt>Agent</dt><dd>{fallbackText(selected.node.asset.agent_id || selected.node.agent?.agent_id)}</dd></div>
                <div><dt>Bootstrap</dt><dd>{bootstrapToken ? "sha256 proof ready" : "loading"}</dd></div>
              </dl>
            ) : <p className="empty">Attach a Windows VM (domain controller, build host, etc.) in Critical Infrastructure to install the AutopilotAgent on it.</p>}
          </div>
        </Panel>

        <Panel title="Full URLs">
          <div className="agent-url-stack">
            <div className="agent-url-row">
              <div>
                <span>MSI</span>
                <code>{msiUrl}</code>
              </div>
              <CopyButton value={msiUrl} label="Copy URL" />
            </div>
            <div className="agent-url-row">
              <div>
                <span>Postinstall</span>
                <code>{postinstallUrl}</code>
              </div>
              <CopyButton value={postinstallUrl} label="Copy URL" />
            </div>
            <div className="agent-url-row">
              <div>
                <span>Seed EXE</span>
                <code>{seedExeUrl}</code>
              </div>
              <CopyButton value={seedExeUrl} label="Copy URL" />
            </div>
          </div>
        </Panel>
      </section>

      <Panel title="Install command">
        <div className="agent-command-panel">
          <pre>{installCommand}</pre>
          <CopyButton value={installCommand} label="Copy command" />
        </div>
      </Panel>

      <BuildHostPanel />
    </PageFrame>
  );
}
