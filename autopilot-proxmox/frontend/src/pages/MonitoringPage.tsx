import { useCallback, useState } from "react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, MetricTerm, Panel } from "../components/ui";
import type { AppBootstrap, OperatorPath, OperatorSignal, SignalsHubResponse } from "../contracts";
import { usePolling } from "../hooks/usePolling";
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
  operator_paths: []
};

function signalToneClass(tone: string | undefined): string {
  return tone && tone !== "neutral" ? `status status--${tone}` : "status";
}

export function MonitoringPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [hub, setHub] = useState<SignalsHubResponse>(emptySignalsHub);
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(true);

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
          <a className="action-link" href="/react/jobs">Jobs</a>
          <a className="action-link" href="/monitoring/settings">Monitoring settings</a>
        </span>
      }
    >
      {error ? <p className="notice" role="status">{error}</p> : null}
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
                    <a href={path.href}>{path.action_label}</a>
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
    </PageFrame>
  );
}
