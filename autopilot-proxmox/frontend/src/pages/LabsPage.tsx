import { useCallback, useRef, useState } from "react";

import { fetchJson, postJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { shortTypeLabel, textValue } from "../utilityModels";
import { formatShortDateTime, type StatusTone } from "../viewModels";

interface LabRow {
  readonly id?: string;
  readonly name?: string;
  readonly short_code?: string;
  readonly group_tag?: string;
  readonly status?: string;
  readonly network_cidr?: string;
  readonly gateway_ip?: string;
  readonly retry_count?: number;
}

interface LabTemplateDefaults {
  readonly name?: string;
  readonly short_code?: string;
  readonly group_tag?: string;
  readonly network_cidr?: string;
  readonly gateway_ip?: string;
  readonly sdn_zone?: string;
  readonly sdn_vnet?: string;
  readonly desktop_count?: number;
  readonly server_count?: number;
}

interface LabTemplate {
  readonly id: string;
  readonly name: string;
  readonly summary?: string;
  readonly defaults?: LabTemplateDefaults;
}

interface LabDraft {
  readonly templateId: string;
  readonly name: string;
  readonly shortCode: string;
  readonly groupTag: string;
  readonly networkCidr: string;
  readonly gatewayIp: string;
  readonly sdnZone: string;
  readonly sdnVnet: string;
  readonly desktopCount: string;
  readonly serverCount: string;
}

interface FindingRow {
  readonly id?: string;
  readonly finding_type?: string;
  readonly severity?: string;
  readonly detail?: string;
}

interface FixActionRow {
  readonly id?: string;
  readonly action_type?: string;
  readonly status?: string;
  readonly detail?: string;
}

interface EventRow {
  readonly id?: string;
  readonly event_type?: string;
  readonly detail?: string;
  readonly created_at?: string;
}

type JsonRecord = Record<string, unknown>;

interface BoundarySurfaceRow {
  readonly id?: string;
  readonly provider?: string;
  readonly kind?: string;
  readonly name?: string;
  readonly ownership?: string;
  readonly source?: string;
  readonly provider_ids?: JsonRecord;
  readonly desired_state?: JsonRecord;
  readonly actual_state?: JsonRecord;
}

type BoundaryRow = BoundarySurfaceRow;

interface BoundaryObjectRow extends BoundarySurfaceRow {
  readonly boundary_id?: string;
}

interface ReservationRow {
  readonly id?: string;
}

interface ReconcileRunRow {
  readonly id?: string;
}

interface LabsPayload {
  readonly templates?: readonly LabTemplate[];
  readonly labs?: readonly LabRow[];
  readonly selected_lab?: LabRow | null;
  readonly findings?: readonly FindingRow[];
  readonly fix_actions?: readonly FixActionRow[];
  readonly events?: readonly EventRow[];
  readonly boundaries?: readonly BoundaryRow[];
  readonly boundary_objects?: readonly BoundaryObjectRow[];
  readonly reservations?: readonly ReservationRow[];
  readonly reconcile_runs?: readonly ReconcileRunRow[];
}

interface NormalizedLabsPayload {
  readonly templates: readonly LabTemplate[];
  readonly labs: readonly LabRow[];
  readonly selected_lab: LabRow | null;
  readonly findings: readonly FindingRow[];
  readonly fix_actions: readonly FixActionRow[];
  readonly events: readonly EventRow[];
  readonly boundaries: readonly BoundaryRow[];
  readonly boundary_objects: readonly BoundaryObjectRow[];
  readonly reservations: readonly ReservationRow[];
  readonly reconcile_runs: readonly ReconcileRunRow[];
}

const emptyPayload: NormalizedLabsPayload = {
  templates: [],
  labs: [],
  selected_lab: null,
  findings: [],
  fix_actions: [],
  events: [],
  boundaries: [],
  boundary_objects: [],
  reservations: [],
  reconcile_runs: []
};

function normalizePayload(payload: LabsPayload | null | undefined): NormalizedLabsPayload {
  return {
    templates: payload?.templates ?? [],
    labs: payload?.labs ?? [],
    selected_lab: payload?.selected_lab ?? null,
    findings: payload?.findings ?? [],
    fix_actions: payload?.fix_actions ?? [],
    events: payload?.events ?? [],
    boundaries: payload?.boundaries ?? [],
    boundary_objects: payload?.boundary_objects ?? [],
    reservations: payload?.reservations ?? [],
    reconcile_runs: payload?.reconcile_runs ?? []
  };
}

const blankDraft: LabDraft = {
  templateId: "",
  name: "",
  shortCode: "",
  groupTag: "",
  networkCidr: "",
  gatewayIp: "",
  sdnZone: "",
  sdnVnet: "",
  desktopCount: "0",
  serverCount: "0"
};

function draftFromTemplate(template: LabTemplate): LabDraft {
  const defaults = template.defaults ?? {};
  return {
    templateId: template.id,
    name: textValue(defaults.name, ""),
    shortCode: textValue(defaults.short_code, ""),
    groupTag: textValue(defaults.group_tag, ""),
    networkCidr: textValue(defaults.network_cidr, ""),
    gatewayIp: textValue(defaults.gateway_ip, ""),
    sdnZone: textValue(defaults.sdn_zone, ""),
    sdnVnet: textValue(defaults.sdn_vnet, ""),
    desktopCount: String(defaults.desktop_count ?? 0),
    serverCount: String(defaults.server_count ?? 0)
  };
}

function countValue(value: string): number {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function sdnIdPart(shortCode: string): string {
  return shortCode.trim().toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function derivedZone(shortCode: string): string {
  const part = sdnIdPart(shortCode);
  return part ? `lab${part}` : "";
}

function derivedVnet(shortCode: string): string {
  const part = sdnIdPart(shortCode);
  return part ? `${part}vnet` : "";
}

function labStatusTone(status: string | undefined): StatusTone {
  const normalized = textValue(status, "unknown").toLowerCase();
  if (["ready", "complete", "completed"].includes(normalized)) {
    return "good";
  }
  if (["blocked", "failed", "error"].includes(normalized)) {
    return "bad";
  }
  if (["validating", "fixing", "reserving", "pending", "queued", "running"].includes(normalized)) {
    return "active";
  }
  return "neutral";
}

function labStatusLabel(status: string | undefined): string {
  return shortTypeLabel(textValue(status, "unknown").toLowerCase());
}

function toneClass(tone: StatusTone): string {
  return tone === "neutral" ? "labs-chip" : `labs-chip labs-chip--${tone}`;
}

function formatStructuredValue(value: JsonRecord | null | undefined): string {
  if (!value || Object.keys(value).length === 0) {
    return "-";
  }
  return JSON.stringify(value);
}

function BoundaryStateTable({ rows }: { readonly rows: readonly BoundaryRow[] }) {
  if (!rows.length) {
    return <p className="empty">No boundary state recorded.</p>;
  }

  return (
    <div className="table-wrap">
      <table className="jobs-table utility-table labs-state-table" aria-label="Boundary current state">
        <thead>
          <tr>
            <th scope="col">Boundary</th>
            <th scope="col">Provider</th>
            <th scope="col">Kind</th>
            <th scope="col">Ownership</th>
            <th scope="col">Source</th>
            <th scope="col">Desired state</th>
            <th scope="col">Actual state</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={textValue(row.id, textValue(row.name))}>
              <td>{textValue(row.name)}</td>
              <td>{textValue(row.provider)}</td>
              <td>{shortTypeLabel(textValue(row.kind))}</td>
              <td>{shortTypeLabel(textValue(row.ownership))}</td>
              <td>{shortTypeLabel(textValue(row.source))}</td>
              <td><code className="labs-state-code">{formatStructuredValue(row.desired_state)}</code></td>
              <td><code className="labs-state-code">{formatStructuredValue(row.actual_state)}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BoundaryObjectStateTable({ rows }: { readonly rows: readonly BoundaryObjectRow[] }) {
  if (!rows.length) {
    return <p className="empty">No boundary objects recorded.</p>;
  }

  return (
    <div className="table-wrap">
      <table className="jobs-table utility-table labs-state-table" aria-label="Boundary object current state">
        <thead>
          <tr>
            <th scope="col">Object</th>
            <th scope="col">Boundary</th>
            <th scope="col">Provider</th>
            <th scope="col">Kind</th>
            <th scope="col">Ownership</th>
            <th scope="col">Provider IDs</th>
            <th scope="col">Desired state</th>
            <th scope="col">Actual state</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={textValue(row.id, textValue(row.name))}>
              <td>{textValue(row.name)}</td>
              <td>{textValue(row.boundary_id)}</td>
              <td>{textValue(row.provider)}</td>
              <td>{shortTypeLabel(textValue(row.kind))}</td>
              <td>{shortTypeLabel(textValue(row.ownership))}</td>
              <td><code className="labs-state-code">{formatStructuredValue(row.provider_ids)}</code></td>
              <td><code className="labs-state-code">{formatStructuredValue(row.desired_state)}</code></td>
              <td><code className="labs-state-code">{formatStructuredValue(row.actual_state)}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function LabsPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<NormalizedLabsPayload>(emptyPayload);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"bad" | "neutral">("neutral");
  const [busyAction, setBusyAction] = useState<"create" | "reconcile" | "fixes" | null>(null);
  const [selectedLabId, setSelectedLabId] = useState(() => new URLSearchParams(window.location.search).get("selected_lab_id") ?? "");
  const [draft, setDraft] = useState<LabDraft>(blankDraft);
  const initialTemplateApplied = useRef(false);

  const load = useCallback(async (labId = selectedLabId) => {
    try {
      const url = labId ? `/api/labs/page?selected_lab_id=${encodeURIComponent(labId)}` : "/api/labs/page";
      const next = normalizePayload(await fetchJson<LabsPayload>(url));
      setPayload(next);
      const firstTemplate = next.templates[0];
      if (!initialTemplateApplied.current && firstTemplate) {
        initialTemplateApplied.current = true;
        setDraft(draftFromTemplate(firstTemplate));
      }
      setSelectedLabId(textValue(next.selected_lab?.id, labId));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load labs page");
    } finally {
      setLoading(false);
    }
  }, [selectedLabId]);

  usePolling(load);

  const selected = payload.selected_lab;
  const selectedTemplate = payload.templates.find((template) => template.id === draft.templateId) ?? null;
  const pendingFixCount = payload.fix_actions.filter((fix) => textValue(fix.status, "").toLowerCase() === "pending").length;

  function applyTemplate(templateId: string) {
    const template = payload.templates.find((item) => item.id === templateId);
    setDraft(template ? draftFromTemplate(template) : { ...blankDraft, templateId: "" });
  }

  function updateDraft(field: keyof LabDraft, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
  }

  function updateShortCode(value: string) {
    const normalized = value.trim().toLowerCase();
    setDraft((current) => {
      const previous = current.shortCode.trim().toLowerCase();
      const nextZone = !current.sdnZone || current.sdnZone === derivedZone(previous) ? derivedZone(normalized) : current.sdnZone;
      const nextVnet = !current.sdnVnet || current.sdnVnet === derivedVnet(previous) ? derivedVnet(normalized) : current.sdnVnet;
      const previousManagedTag = `${previous.toUpperCase()}-Managed`;
      const nextGroupTag = !current.groupTag || current.groupTag === previousManagedTag ? `${normalized.toUpperCase()}-Managed` : current.groupTag;
      return {
        ...current,
        shortCode: normalized,
        groupTag: nextGroupTag,
        sdnZone: nextZone,
        sdnVnet: nextVnet
      };
    });
  }

  async function createLab() {
    const shortCode = draft.shortCode.trim().toLowerCase();
    const networkCidr = draft.networkCidr.trim();
    const zone = draft.sdnZone.trim() || derivedZone(shortCode);
    const vnet = draft.sdnVnet.trim() || derivedVnet(shortCode);
    setBusyAction("create");
    setMessage("");
    try {
      await postJson("/api/labs", {
        template_id: draft.templateId,
        name: draft.name.trim(),
        short_code: shortCode,
        group_tag: draft.groupTag.trim(),
        network_cidr: networkCidr,
        gateway_ip: draft.gatewayIp.trim(),
        network_mode: "sdn",
        sdn_zone: zone,
        sdn_vnet: vnet,
        sdn_subnet: networkCidr,
        desktop_count: countValue(draft.desktopCount),
        server_count: countValue(draft.serverCount)
      });
      setMessage("Lab created.");
      setMessageTone("neutral");
      await load();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Create lab failed");
      setMessageTone("bad");
    } finally {
      setBusyAction(null);
    }
  }

  async function reconcileLab() {
    if (!selected?.id) {
      return;
    }
    setBusyAction("reconcile");
    setMessage("");
    try {
      await postJson(`/api/labs/${selected.id}/reconcile`);
      setMessage("Reconcile queued.");
      setMessageTone("neutral");
      await load();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Reconcile failed");
      setMessageTone("bad");
    } finally {
      setBusyAction(null);
    }
  }

  async function runPendingFixes() {
    if (!selected?.id) {
      return;
    }
    setBusyAction("fixes");
    setMessage("");
    try {
      await postJson(`/api/labs/${selected.id}/fixes/run-pending`);
      setMessage("Pending fixes executed.");
      setMessageTone("neutral");
      await load();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Pending fixes failed");
      setMessageTone("bad");
    } finally {
      setBusyAction(null);
    }
  }

  async function selectLab(lab: LabRow) {
    const labId = textValue(lab.id, "").trim();
    if (!labId || labId === selectedLabId) {
      return;
    }
    setSelectedLabId(labId);
    window.history.replaceState(null, "", `${window.location.pathname}?selected_lab_id=${encodeURIComponent(labId)}`);
    setLoading(true);
    await load(labId);
  }

  return (
    <PageFrame bootstrap={bootstrap} title="Labs" section="Deploy" path="/react/labs">
      {loading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading labs</span>
          <div className="load-strip__track" role="progressbar" aria-label="labs loading"><span /></div>
        </div>
      ) : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {message ? <p className={messageTone === "bad" ? "notice notice--bad" : "notice"} role="status">{message}</p> : null}

      <section className="metric-strip" aria-label="Lab metrics">
        <Metric label="Labs" value={String(payload.labs.length)} />
        <Metric label="Status" value={labStatusLabel(selected?.status)} tone={labStatusTone(selected?.status)} />
        <Metric label="Findings" value={String(payload.findings.length)} tone={payload.findings.length > 0 ? "bad" : "good"} />
        <Metric label="Pending fixes" value={String(pendingFixCount)} tone={pendingFixCount > 0 ? "active" : "neutral"} />
      </section>

      <div className="labs-layout">
        <Panel title="Create managed lab">
          <form
            className="labs-form"
            onSubmit={(event) => {
              event.preventDefault();
              void createLab();
            }}
          >
            <p className="labs-form__hint">
              Default workstation names follow short-code-role-index, for example <code>ntt01-wks-001</code>.
            </p>
            <div className="utility-field-grid">
              <label className="utility-field utility-field--wide">
                <span>Lab template</span>
                <select
                  aria-label="Lab template"
                  value={draft.templateId}
                  onChange={(event) => { applyTemplate(event.target.value); }}
                >
                  {payload.templates.length ? null : <option value="">Custom lab</option>}
                  {payload.templates.map((template) => (
                    <option key={template.id} value={template.id}>{template.name}</option>
                  ))}
                </select>
                {selectedTemplate?.summary ? <small>{selectedTemplate.summary}</small> : null}
              </label>
              <label className="utility-field utility-field--wide">
                <span>Lab name</span>
                <input
                  name="name"
                  aria-label="Lab name"
                  value={draft.name}
                  onChange={(event) => { updateDraft("name", event.target.value); }}
                  required
                />
              </label>
              <label className="utility-field">
                <span>Short code</span>
                <input
                  name="short_code"
                  aria-label="Short code"
                  value={draft.shortCode}
                  onChange={(event) => { updateShortCode(event.target.value); }}
                  required
                />
              </label>
              <label className="utility-field">
                <span>Group tag</span>
                <input
                  name="group_tag"
                  aria-label="Group tag"
                  value={draft.groupTag}
                  onChange={(event) => { updateDraft("groupTag", event.target.value); }}
                  required
                />
              </label>
              <label className="utility-field">
                <span>Subnet CIDR</span>
                <input
                  name="network_cidr"
                  aria-label="Subnet CIDR"
                  value={draft.networkCidr}
                  onChange={(event) => { updateDraft("networkCidr", event.target.value); }}
                  required
                />
              </label>
              <label className="utility-field">
                <span>Gateway IP</span>
                <input
                  name="gateway_ip"
                  aria-label="Gateway IP"
                  value={draft.gatewayIp}
                  onChange={(event) => { updateDraft("gatewayIp", event.target.value); }}
                />
              </label>
              <label className="utility-field">
                <span>Desktop count</span>
                <input
                  type="number"
                  name="desktop_count"
                  aria-label="Desktop count"
                  value={draft.desktopCount}
                  min="0"
                  max="500"
                  onChange={(event) => { updateDraft("desktopCount", event.target.value); }}
                />
              </label>
              <label className="utility-field">
                <span>Server count</span>
                <input
                  type="number"
                  name="server_count"
                  aria-label="Server count"
                  value={draft.serverCount}
                  min="0"
                  max="500"
                  onChange={(event) => { updateDraft("serverCount", event.target.value); }}
                />
              </label>
              <label className="utility-field">
                <span>SDN zone</span>
                <input
                  name="sdn_zone"
                  aria-label="SDN zone"
                  value={draft.sdnZone}
                  onChange={(event) => { updateDraft("sdnZone", event.target.value); }}
                  placeholder="labntt01"
                />
              </label>
              <label className="utility-field">
                <span>SDN VNet</span>
                <input
                  name="sdn_vnet"
                  aria-label="SDN VNet"
                  value={draft.sdnVnet}
                  onChange={(event) => { updateDraft("sdnVnet", event.target.value); }}
                  placeholder="ntt01vnet"
                />
              </label>
            </div>
            <div className="labs-form__actions">
              <button className="utility-button" type="submit" disabled={busyAction !== null}>
                {busyAction === "create" ? "Creating..." : "Create lab"}
              </button>
            </div>
          </form>
        </Panel>

        <Panel title="Lab roster">
          {payload.labs.length ? (
            <ul className="labs-roster" aria-label="Managed labs roster">
              {payload.labs.map((lab) => {
                const isSelected = selected?.id === lab.id;
                return (
                  <li key={textValue(lab.id, textValue(lab.name))}>
                    <button
                      className="labs-roster__button"
                      type="button"
                      aria-current={isSelected ? "true" : undefined}
                      aria-label={`Select ${textValue(lab.name, "lab")}`}
                      onClick={() => { void selectLab(lab); }}
                    >
                      <div className="labs-row__head">
                        <strong>{textValue(lab.name)}</strong>
                        <span className={toneClass(labStatusTone(lab.status))}>{labStatusLabel(lab.status)}</span>
                      </div>
                      <div className="labs-roster__detail">
                        <code>{textValue(lab.short_code)}</code>
                        <span>{textValue(lab.network_cidr)}</span>
                        <span>{textValue(lab.group_tag)}</span>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="empty">No managed labs yet.</p>
          )}
        </Panel>
      </div>

      <section className="labs-grid" aria-label="Lab reconcile state">
        <Panel
          title="Selected lab"
          action={selected ? (
            <div className="utility-form-actions">
              <button className="utility-button" type="button" onClick={() => { void reconcileLab(); }} disabled={busyAction !== null}>
                {busyAction === "reconcile" ? "Reconciling..." : "Reconcile"}
              </button>
              <button className="utility-button" type="button" onClick={() => { void runPendingFixes(); }} disabled={busyAction !== null}>
                {busyAction === "fixes" ? "Running fixes..." : "Run pending fixes"}
              </button>
            </div>
          ) : undefined}
        >
          {selected ? (
            <div className="labs-selected">
              <div className="labs-selected__head">
                <div>
                  <p>Managed lab intent</p>
                  <h3>{textValue(selected.name)}</h3>
                </div>
                <span className={toneClass(labStatusTone(selected.status))}>{labStatusLabel(selected.status)}</span>
              </div>
              <dl className="utility-definition-grid">
                <div><dt>Short code</dt><dd>{textValue(selected.short_code)}</dd></div>
                <div><dt>Group tag</dt><dd>{textValue(selected.group_tag)}</dd></div>
                <div><dt>Subnet</dt><dd>{textValue(selected.network_cidr)}</dd></div>
                <div><dt>Gateway</dt><dd>{textValue(selected.gateway_ip)}</dd></div>
                <div><dt>Boundaries</dt><dd>{String(payload.boundaries.length)}</dd></div>
                <div><dt>Boundary objects</dt><dd>{String(payload.boundary_objects.length)}</dd></div>
                <div><dt>Reservations</dt><dd>{String(payload.reservations.length)}</dd></div>
                <div><dt>Runs</dt><dd>{String(payload.reconcile_runs.length)}</dd></div>
              </dl>
              <p className="labs-form__hint">
                Reconcile compares Proxmox SDN state to this lab intent and records fix actions when drift is found.
              </p>
            </div>
          ) : (
            <p className="empty">Create a lab to start reconciliation.</p>
          )}
        </Panel>

        <div className="labs-grid__wide">
          <Panel title="Boundary current state">
            <BoundaryStateTable rows={payload.boundaries} />
          </Panel>
        </div>

        <div className="labs-grid__wide">
          <Panel title="Boundary object current state">
            <BoundaryObjectStateTable rows={payload.boundary_objects} />
          </Panel>
        </div>

        <Panel title="Findings">
          {payload.findings.length ? (
            payload.findings.map((finding) => (
              <article className="lab-state-row" key={textValue(finding.id, textValue(finding.finding_type))}>
                <div className="labs-row__head">
                  <strong>{shortTypeLabel(textValue(finding.finding_type, "finding"))}</strong>
                  <span className={toneClass(labStatusTone(finding.severity))}>{shortTypeLabel(textValue(finding.severity, "open"))}</span>
                </div>
                <p>{textValue(finding.detail)}</p>
              </article>
            ))
          ) : (
            <p className="empty">No open findings.</p>
          )}
        </Panel>

        <Panel title="Fix actions">
          {payload.fix_actions.length ? (
            payload.fix_actions.map((fix) => (
              <article className="lab-state-row" key={textValue(fix.id, textValue(fix.action_type))}>
                <div className="labs-row__head">
                  <strong>{shortTypeLabel(textValue(fix.action_type, "fix"))}</strong>
                  <span className={toneClass(labStatusTone(fix.status))}>{labStatusLabel(fix.status)}</span>
                </div>
                <p>{textValue(fix.detail)}</p>
              </article>
            ))
          ) : (
            <p className="empty">No fix actions.</p>
          )}
        </Panel>

        <Panel title="Timeline">
          {payload.events.length ? (
            payload.events.map((event) => (
              <article className="lab-state-row" key={textValue(event.id, textValue(event.created_at))}>
                <div className="labs-row__head">
                  <strong>{shortTypeLabel(textValue(event.event_type, "event"))}</strong>
                  <span>{formatShortDateTime(event.created_at)}</span>
                </div>
                <p>{textValue(event.detail)}</p>
              </article>
            ))
          ) : (
            <p className="empty">No events yet.</p>
          )}
        </Panel>
      </section>
    </PageFrame>
  );
}
