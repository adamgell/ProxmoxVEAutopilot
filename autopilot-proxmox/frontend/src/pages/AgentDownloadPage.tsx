import { useEffect, useMemo, useState } from "react";
import { Copy, Download } from "lucide-react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Panel } from "../components/ui";
import type {
  AgentDownloadBootstrapTokenResponse,
  AppBootstrap,
  LabBubbleInfrastructureNode,
  VmsFleetResponse
} from "../contracts";
import { fallbackText, vmDisplayName } from "../viewModels";

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

function controllerAddress(node: LabBubbleInfrastructureNode): string {
  return node.agent?.primary_ipv4 || node.vm?.ip_address || "";
}

/**
 * The autopilot controller URL is the address the AutopilotAgent reports
 * back to (the Flask server running this page). It is NOT the IP of any
 * Critical Infrastructure VM. window.location.origin is correct here --
 * picking an install-target VM must not overwrite the controller URL.
 */
function defaultControllerUrl(): string {
  return window.location.origin;
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
    </PageFrame>
  );
}
