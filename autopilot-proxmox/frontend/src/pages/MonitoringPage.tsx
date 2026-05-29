import { useCallback, useState } from "react";

import { fetchJson, postJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, MetricTerm, Panel } from "../components/ui";
import type {
  AppBootstrap,
  DeploymentBottleneck,
  DeploymentRunDigest,
  FleetSignalRow,
  LifecycleLane,
  OperatorPath,
  OperatorSignal,
  RuntimeContainer,
  ServiceLogsResponse,
  ServiceHealth,
  SignalsHubResponse
} from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { reactHrefForUiPath } from "../routes";
import {
  buildSignalMetrics,
  fallbackText,
  formatShortDateTime,
  rankedSignalPaths,
  statusLabel
} from "../viewModels";

const emptySignalsHub: SignalsHubResponse = {
  generated_at: "",
  build: {},
  source_health: { runtime_available: true },
  metrics: [],
  signals: [],
  operator_paths: [],
  lifecycle_lanes: [],
  deployment_health: {
    summary: { total: 0, failed: 0 },
    active: [],
    recent_completions: [],
    bottlenecks: []
  },
  services: [],
  runtime: { available: true, error: "", containers: [] },
  fleet_attention: []
};

function signalToneClass(tone: string | undefined): string {
  return tone && tone !== "neutral" ? `status status--${tone}` : "status";
}

function compactNumber(value: number | null | undefined): string {
  return typeof value === "number" ? String(value) : "-";
}

function runLabel(run: DeploymentRunDigest): string {
  return run.deployment_key || run.deployment_type || "deployment";
}

function secondsLabel(value: number | null | undefined): string {
  return typeof value === "number" ? `${String(value)}s` : "-";
}

function serviceLabel(service: ServiceHealth): string {
  return service.service_id || service.service || service.service_type || "service";
}

export function MonitoringPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [hub, setHub] = useState<SignalsHubResponse>(emptySignalsHub);
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [selectedLogContainer, setSelectedLogContainer] = useState("");
  const [logLines, setLogLines] = useState<readonly string[]>([]);
  const [logError, setLogError] = useState("");
  const [logsLoading, setLogsLoading] = useState(false);
  const [sweepState, setSweepState] = useState<"idle" | "queueing" | "queued" | "failed">("idle");
  const [sweepMessage, setSweepMessage] = useState("");

  const load = useCallback(async () => {
    try {
      const signals = await fetchJson<SignalsHubResponse>("/api/monitoring/signals");
      setHub(signals);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load signals");
    } finally {
      setIsLoading(false);
    }
  }, []);

  usePolling(load);

  const loadLogs = useCallback(async (container: string) => {
    setSelectedLogContainer(container);
    setLogsLoading(true);
    try {
      const payload = await fetchJson<ServiceLogsResponse>(
        `/api/monitoring/service-logs?tail=180&container=${encodeURIComponent(container)}`
      );
      setLogLines(payload.lines);
      setLogError("");
    } catch (err) {
      setLogLines([]);
      setLogError(err instanceof Error ? err.message : "Failed to load logs");
    } finally {
      setLogsLoading(false);
    }
  }, []);

  const queueSweepNow = useCallback(async () => {
    setSweepState("queueing");
    setSweepMessage("");
    try {
      await postJson<{ readonly ok?: boolean }>("/api/monitoring/sweep-now");
      setSweepState("queued");
      setSweepMessage("Sweep queued.");
      await load();
    } catch (err) {
      setSweepState("failed");
      setSweepMessage(err instanceof Error ? err.message : "Sweep request failed.");
    }
  }, [load]);

  const metrics = hub.metrics.length ? hub.metrics : buildSignalMetrics(hub);
  const rankedPaths = rankedSignalPaths(hub.operator_paths);
  const selectedSignal = hub.signals[0];
  const selectedPath = rankedPaths[0];

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Signals Hub"
      section="Observe"
      path="/react/monitoring"
      action={
        <span className="action-cluster">
          <button
            className="action-link"
            type="button"
            disabled={sweepState === "queueing"}
            onClick={() => { void queueSweepNow(); }}
          >
            {sweepState === "queueing" ? "Queueing sweep" : "Sweep now"}
          </button>
          <a className="action-link" href="/react/jobs">Jobs</a>
          <a className="action-link" href="/react/monitoring/settings">Monitoring settings</a>
        </span>
      }
    >
      {error ? <p className="notice" role="status">{error}</p> : null}
      {sweepMessage ? (
        <p className={sweepState === "failed" ? "notice notice--bad" : "notice"} role="status">{sweepMessage}</p>
      ) : null}
      {isLoading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading signals</span>
          <div className="load-strip__track" role="progressbar" aria-label="Signals loading">
            <span />
          </div>
        </div>
      ) : null}
      {!isLoading ? (
        <section className="metric-strip metric-strip--signals" aria-label="Signals Hub metrics">
          {metrics.map((item) => (
            <Metric key={item.label} label={item.label} value={item.value} tone={item.tone} />
          ))}
        </section>
      ) : null}

      {!isLoading ? (
        <section className="signals-layout">
          <Panel title="Signal families">
            {hub.signals.length ? (
              <ul className="signal-list">
                {hub.signals.map((signal: OperatorSignal) => (
                  <li key={signal.id}>
                    <span className={signalToneClass(signal.tone)}>{statusLabel(signal.status)}</span>
                    <div>
                      <strong>{signal.label}</strong>
                      <p>{signal.summary}</p>
                      <small>{signal.source || signal.family}</small>
                    </div>
                    <span>{fallbackText(signal.count)}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="empty">No signals collected.</p>
            )}
          </Panel>

          <Panel title="Ranked operator paths">
            {rankedPaths.length ? (
              <ul className="path-list">
                {rankedPaths.map((path: OperatorPath) => (
                  <li key={path.id}>
                    <span className={signalToneClass(path.tone)}>{statusLabel(path.status)}</span>
                    <div>
                      <strong>{path.label}</strong>
                      <p>{path.summary}</p>
                      <small>{path.source || "Signals Hub"}</small>
                    </div>
                    <a href={reactHrefForUiPath(path.href)}>{path.action_label}</a>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="empty">No operator paths ranked.</p>
            )}
          </Panel>

          <Panel title="Selected signal">
            <div className="signal-detail">
              <dl className="fleet-grid fleet-grid--four">
                <MetricTerm label="Runtime" value={hub.source_health.runtime_available ? "up" : "down"} />
                <MetricTerm label="Setup" value={fallbackText(hub.source_health.setup_health)} />
                <MetricTerm label="Keytab" value={fallbackText(hub.source_health.keytab_status)} />
                <MetricTerm label="Generated" value={formatShortDateTime(hub.generated_at)} />
              </dl>
              {selectedSignal ? (
                <div className="detail-callout">
                  <span className={signalToneClass(selectedSignal.tone)}>{statusLabel(selectedSignal.status)}</span>
                  <div>
                    <strong>{selectedSignal.label}</strong>
                    <p>{selectedSignal.summary}</p>
                  </div>
                </div>
              ) : null}
              {selectedPath ? (
                <div className="detail-callout">
                  <span className={signalToneClass(selectedPath.tone)}>{String(selectedPath.priority)}</span>
                  <div>
                    <strong>{selectedPath.label}</strong>
                    <p>{selectedPath.summary}</p>
                  </div>
                </div>
              ) : null}
            </div>
          </Panel>
        </section>
      ) : null}

      {!isLoading ? (
        <section className="signals-detail-grid" aria-label="Monitoring details">
          <Panel title="Deployment speed">
            <dl className="fleet-grid fleet-grid--six">
              <MetricTerm label="Active" value={String(hub.deployment_health.summary.active ?? 0)} />
              <MetricTerm label="Stuck" value={String(hub.deployment_health.summary.stuck ?? 0)} />
              <MetricTerm label="Failed" value={String(hub.deployment_health.summary.failed)} />
              <MetricTerm label="Regressed" value={String(hub.deployment_health.summary.regressed ?? 0)} />
              <MetricTerm label="Median" value={compactNumber(hub.deployment_health.summary.median_completion_seconds)} />
              <MetricTerm label="P95" value={compactNumber(hub.deployment_health.summary.p95_completion_seconds)} />
            </dl>
            <div className="monitoring-columns">
              <div>
                <h3>Active</h3>
                {hub.deployment_health.active.length ? (
                  <ul className="compact-list">
                    {hub.deployment_health.active.map((run: DeploymentRunDigest) => (
                      <li key={runLabel(run)}>
                        <span className={signalToneClass(run.health)}>{statusLabel(run.health || run.state)}</span>
                        <div>
                          <strong>{runLabel(run)}</strong>
                          <p>{fallbackText(run.current_phase)} / {compactNumber(run.elapsed_seconds)}s / {fallbackText(run.next_expected_evidence)}</p>
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="empty">No active deployment phases.</p>
                )}
              </div>
              <div>
                <h3>Recent</h3>
                {hub.deployment_health.recent_completions.length ? (
                  <ul className="compact-list">
                    {hub.deployment_health.recent_completions.map((run: DeploymentRunDigest) => (
                      <li key={`${runLabel(run)}-${String(run.duration_seconds ?? "")}`}>
                        <span className={signalToneClass(run.health)}>{statusLabel(run.health || run.state)}</span>
                        <div>
                          <strong>{runLabel(run)}</strong>
                          <p>{secondsLabel(run.duration_seconds)} total / {fallbackText(run.slowest_phase)} / {secondsLabel(run.slowest_phase_seconds)}</p>
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="empty">No completed deployment timing samples.</p>
                )}
              </div>
              <div>
                <h3>Bottlenecks</h3>
                {hub.deployment_health.bottlenecks.length ? (
                  <ul className="compact-list">
                    {hub.deployment_health.bottlenecks.map((item: DeploymentBottleneck) => (
                      <li key={`${item.deployment_type || "deployment"}-${item.phase_key || item.phase_label || "phase"}`}>
                        <span className={signalToneClass(item.health)}>{statusLabel(item.health)}</span>
                        <div>
                          <strong>{fallbackText(item.phase_label)}</strong>
                          <p>{fallbackText(item.deployment_type)} / {fallbackText(item.phase_key)} / {compactNumber(item.count)} affected</p>
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="empty">No bottleneck phases flagged.</p>
                )}
              </div>
            </div>
          </Panel>

          <Panel title="Lifecycle lanes">
            {hub.lifecycle_lanes.length ? (
              <ul className="lane-list">
                {hub.lifecycle_lanes.map((lane: LifecycleLane) => (
                  <li key={lane.id}>
                    <span className={signalToneClass(lane.tone)}>{statusLabel(lane.status)}</span>
                    <div>
                      <strong>{lane.label}</strong>
                      <p>{lane.detail || "-"}</p>
                    </div>
                    <b>{lane.value}</b>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="empty">No lifecycle lanes collected.</p>
            )}
          </Panel>

          <Panel title="Service health">
            {hub.services.length ? (
              <ul className="compact-list">
                {hub.services.map((service: ServiceHealth) => (
                  <li key={serviceLabel(service)}>
                    <span className={signalToneClass(service.status)}>{statusLabel(service.status)}</span>
                    <div>
                      <strong>{serviceLabel(service)}</strong>
                      <p>{fallbackText(service.detail || service.message)} / {fallbackText(service.age_seconds)}s heartbeat</p>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="empty">No service heartbeat rows.</p>
            )}
          </Panel>

          <Panel title="Runtime containers">
            {!hub.runtime.available ? <p className="notice" role="status">{fallbackText(hub.runtime.error)}</p> : null}
            {hub.runtime.containers.length ? (
              <div className="table-wrap">
                <table className="jobs-table jobs-table--compact runtime-table">
                  <thead>
                    <tr>
                      <th scope="col">Container</th>
                      <th scope="col">Service</th>
                      <th scope="col">Status</th>
                      <th scope="col">Health</th>
                      <th scope="col">Logs</th>
                    </tr>
                  </thead>
                  <tbody>
                    {hub.runtime.containers.map((container: RuntimeContainer) => (
                      <tr key={container.name}>
                        <td>{container.name}</td>
                        <td>{container.service}</td>
                        <td><span className={signalToneClass(container.status)}>{statusLabel(container.status)}</span></td>
                        <td>{container.health ? <span className={signalToneClass(container.health)}>{statusLabel(container.health)}</span> : "-"}</td>
                        <td>
                          <button
                            type="button"
                            className="table-action"
                            onClick={() => {
                              void loadLogs(container.name);
                            }}
                          >
                            Tail
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="log-panel">
                  <div className="log-panel__head">
                    <strong>{selectedLogContainer ? `Recent logs for ${selectedLogContainer}` : "Select a container"}</strong>
                    {logsLoading ? <span>Loading</span> : null}
                  </div>
                  {logError ? <p className="notice" role="status">{logError}</p> : null}
                  <pre className={logLines.length ? "log-output" : "log-output is-muted"}>
                    {logLines.length ? logLines.join("\n") : "No container selected."}
                  </pre>
                </div>
              </div>
            ) : (
              <p className="empty">No Autopilot containers detected.</p>
            )}
          </Panel>

          <Panel title="Fleet attention">
            {hub.fleet_attention.length ? (
              <ul className="fleet-attention-list">
                {hub.fleet_attention.map((row: FleetSignalRow) => (
                  <li key={row.vmid}>
                    <span className={signalToneClass(row.tone)}>{row.lifecycle}</span>
                    <div>
                      <strong>{row.vm_name}</strong>
                      <p>VMID {row.vmid} / {fallbackText(row.node)} / {fallbackText(row.pve_status)} / {fallbackText(row.windows)}</p>
                      <small>AD {fallbackText(row.ad)} / Entra {fallbackText(row.entra)} / Intune {fallbackText(row.intune)}</small>
                    </div>
                    <a href={reactHrefForUiPath(row.href)}>Inspect</a>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="empty">No fleet lifecycle rows need review.</p>
            )}
          </Panel>
        </section>
      ) : null}
    </PageFrame>
  );
}
