import { useCallback, useMemo, useState } from "react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { textValue } from "../utilityModels";
import { formatShortDateTime, statusClass, statusLabel } from "../viewModels";

interface TrackingSummary {
  readonly complete?: number;
  readonly running?: number;
  readonly blockers?: number;
  readonly total?: number;
  readonly percent?: number;
}

interface TrackingRun {
  readonly run_id?: string;
  readonly name?: string;
  readonly target?: string;
}

interface TrackingItem {
  readonly item_id?: string;
  readonly label?: string;
  readonly category?: string;
  readonly description?: string;
  readonly source?: string;
  readonly status?: string;
  readonly target?: string;
  readonly detail?: string;
  readonly evidence?: unknown;
}

interface TrackingEvent {
  readonly created_at?: string;
  readonly item_id?: string;
  readonly status?: string;
  readonly detail?: string;
}

interface InstallTrackingPayload {
  readonly tracking: {
    readonly summary?: TrackingSummary;
    readonly run?: TrackingRun;
    readonly runs?: readonly TrackingRun[];
    readonly items?: readonly TrackingItem[];
    readonly events?: readonly TrackingEvent[];
  };
}

function Badge({ status }: { readonly status: string | undefined }) {
  const normalized = textValue(status, "pending");
  return <span className={statusClass(normalized)}>{statusLabel(normalized)}</span>;
}

export function InstallTrackingPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<InstallTrackingPayload>({ tracking: {} });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const endpoint = `/api/install-tracking/page${window.location.search}`;

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<InstallTrackingPayload>(endpoint));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load install tracking");
    } finally {
      setLoading(false);
    }
  }, [endpoint]);

  usePolling(load);

  const tracking = payload.tracking;
  const summary = tracking.summary ?? {};
  const run = tracking.run ?? {};
  const needle = filter.trim().toLowerCase();
  const items = useMemo(() => (tracking.items ?? []).filter((item) => {
    if (!needle) {
      return true;
    }
    return [item.label, item.category, item.description, item.source, item.status, item.target, item.detail]
      .map((value) => textValue(value).toLowerCase())
      .join(" ")
      .includes(needle);
  }), [needle, tracking.items]);

  return (
    <PageFrame bootstrap={bootstrap} title="Deployment Readiness" section="Observe" path="/react/install-tracking">
      {loading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading deployment readiness</span>
          <div className="load-strip__track" role="progressbar" aria-label="Deployment readiness loading"><span /></div>
        </div>
      ) : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}

      <section className="metric-strip" aria-label="Deployment readiness metrics">
        <Metric label={`Complete ${textValue(summary.percent, "0")}%`} value={textValue(summary.complete, "0")} />
        <Metric label="In progress" value={textValue(summary.running, "0")} />
        <Metric label="Blockers" value={textValue(summary.blockers, "0")} />
        <Metric label="Required gates" value={textValue(summary.total, "0")} />
      </section>

      <Panel title="Readiness Scope">
        <dl className="utility-definition-grid">
          <div><dt>Scope</dt><dd>{textValue(run.name)} / {textValue(run.target, "target not set")}</dd></div>
          <div><dt>Evidence set</dt><dd><code>{textValue(run.run_id)}</code></dd></div>
          <div><dt>Tracked runs</dt><dd>{textValue(tracking.runs?.length, "0")}</dd></div>
        </dl>
        <label className="utility-field utility-field--wide">
          <span>Filter install gates</span>
          <input type="search" value={filter} placeholder="category, VMID, source, status, evidence" onChange={(event) => {
            setFilter(event.target.value);
          }} />
        </label>
      </Panel>

      <Panel title="Readiness Gates">
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label="Readiness gates">
            <thead><tr><th>Gate</th><th>Status</th><th>Target</th><th>Evidence</th></tr></thead>
            <tbody>
              {items.map((item, index) => (
                <tr key={textValue(item.item_id, String(index))}>
                  <td><strong>{textValue(item.label)}</strong><br /><small>{textValue(item.category)} / {textValue(item.description)}</small><br /><small>Source: <code>{textValue(item.source, "operator")}</code></small></td>
                  <td><Badge status={item.status} /></td>
                  <td>{textValue(item.target, "not set")}</td>
                  <td>{textValue(item.detail)}{item.evidence ? <pre>{JSON.stringify(item.evidence)}</pre> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Recent Updates">
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label="Install tracking updates">
            <thead><tr><th>Time</th><th>Gate</th><th>Status</th><th>Detail</th></tr></thead>
            <tbody>
              {(tracking.events ?? []).map((event, index) => (
                <tr key={`${textValue(event.item_id)}-${String(index)}`}>
                  <td>{formatShortDateTime(event.created_at)}</td>
                  <td><code>{textValue(event.item_id)}</code></td>
                  <td><Badge status={event.status} /></td>
                  <td>{textValue(event.detail)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </PageFrame>
  );
}
