import { useCallback, useState } from "react";

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

interface BoundaryRow {
  readonly id?: string;
}

interface ReservationRow {
  readonly id?: string;
}

interface ReconcileRunRow {
  readonly id?: string;
}

interface LabsPayload {
  readonly labs?: readonly LabRow[];
  readonly selected_lab?: LabRow | null;
  readonly findings?: readonly FindingRow[];
  readonly fix_actions?: readonly FixActionRow[];
  readonly events?: readonly EventRow[];
  readonly boundaries?: readonly BoundaryRow[];
  readonly reservations?: readonly ReservationRow[];
  readonly reconcile_runs?: readonly ReconcileRunRow[];
}

interface NormalizedLabsPayload {
  readonly labs: readonly LabRow[];
  readonly selected_lab: LabRow | null;
  readonly findings: readonly FindingRow[];
  readonly fix_actions: readonly FixActionRow[];
  readonly events: readonly EventRow[];
  readonly boundaries: readonly BoundaryRow[];
  readonly reservations: readonly ReservationRow[];
  readonly reconcile_runs: readonly ReconcileRunRow[];
}

const emptyPayload: NormalizedLabsPayload = {
  labs: [],
  selected_lab: null,
  findings: [],
  fix_actions: [],
  events: [],
  boundaries: [],
  reservations: [],
  reconcile_runs: []
};

function normalizePayload(payload: LabsPayload | null | undefined): NormalizedLabsPayload {
  return {
    labs: payload?.labs ?? [],
    selected_lab: payload?.selected_lab ?? null,
    findings: payload?.findings ?? [],
    fix_actions: payload?.fix_actions ?? [],
    events: payload?.events ?? [],
    boundaries: payload?.boundaries ?? [],
    reservations: payload?.reservations ?? [],
    reconcile_runs: payload?.reconcile_runs ?? []
  };
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

export function LabsPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<NormalizedLabsPayload>(emptyPayload);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"bad" | "neutral">("neutral");
  const [busyAction, setBusyAction] = useState<"create" | "reconcile" | "fixes" | null>(null);

  const load = useCallback(async () => {
    try {
      const next = normalizePayload(await fetchJson<LabsPayload>("/api/labs/page"));
      setPayload(next);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load labs page");
    } finally {
      setLoading(false);
    }
  }, []);

  usePolling(load);

  const selected = payload.selected_lab;
  const pendingFixCount = payload.fix_actions.filter((fix) => textValue(fix.status, "").toLowerCase() === "pending").length;

  async function createLab(form: FormData) {
    const shortCode = textValue(form.get("short_code"), "").trim().toLowerCase();
    const networkCidr = textValue(form.get("network_cidr"), "").trim();
    const zone = textValue(form.get("sdn_zone"), `lab-${shortCode}`).trim() || `lab-${shortCode}`;
    const vnet = textValue(form.get("sdn_vnet"), `${shortCode}-vnet`).trim() || `${shortCode}-vnet`;
    setBusyAction("create");
    setMessage("");
    try {
      await postJson("/api/labs", {
        name: textValue(form.get("name"), "").trim(),
        short_code: shortCode,
        group_tag: textValue(form.get("group_tag"), "").trim(),
        network_cidr: networkCidr,
        gateway_ip: textValue(form.get("gateway_ip"), "").trim(),
        network_mode: "sdn",
        sdn_zone: zone,
        sdn_vnet: vnet,
        sdn_subnet: networkCidr
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
              void createLab(new FormData(event.currentTarget));
            }}
          >
            <p className="labs-form__hint">
              Default workstation names follow short-code-role-index, for example <code>ntt01-wks-001</code>.
            </p>
            <div className="utility-field-grid">
              <label className="utility-field utility-field--wide">
                <span>Lab name</span>
                <input name="name" aria-label="Lab name" required />
              </label>
              <label className="utility-field">
                <span>Short code</span>
                <input name="short_code" aria-label="Short code" required />
              </label>
              <label className="utility-field">
                <span>Group tag</span>
                <input name="group_tag" aria-label="Group tag" required />
              </label>
              <label className="utility-field">
                <span>Subnet CIDR</span>
                <input name="network_cidr" aria-label="Subnet CIDR" required />
              </label>
              <label className="utility-field">
                <span>Gateway IP</span>
                <input name="gateway_ip" aria-label="Gateway IP" />
              </label>
              <label className="utility-field">
                <span>SDN zone</span>
                <input name="sdn_zone" aria-label="SDN zone" placeholder="lab-ntt01" />
              </label>
              <label className="utility-field">
                <span>SDN VNet</span>
                <input name="sdn_vnet" aria-label="SDN VNet" placeholder="ntt01-vnet" />
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
              {payload.labs.map((lab) => (
                <li key={textValue(lab.id, textValue(lab.name))}>
                  <div className="labs-row__head">
                    <strong>{textValue(lab.name)}</strong>
                    <span className={toneClass(labStatusTone(lab.status))}>{labStatusLabel(lab.status)}</span>
                  </div>
                  <div className="labs-roster__detail">
                    <code>{textValue(lab.short_code)}</code>
                    <span>{textValue(lab.network_cidr)}</span>
                    <span>{textValue(lab.group_tag)}</span>
                  </div>
                </li>
              ))}
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
