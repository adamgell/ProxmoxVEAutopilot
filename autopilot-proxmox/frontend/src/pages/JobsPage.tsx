import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { JobsTable, Metric, Panel } from "../components/ui";
import type { AppBootstrap, JobTableRow, JobsLivePayload } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { connectJobsLive } from "../liveSocket";
import { jobTarget, summarizeJobs } from "../viewModels";

export function JobsPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [jobs, setJobs] = useState<readonly JobTableRow[]>([]);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");
  const [socketState, setSocketState] = useState("closed");

  const load = useCallback(async () => {
    try {
      setJobs(await fetchJson<readonly JobTableRow[]>("/api/jobs"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load jobs");
    }
  }, []);

  usePolling(load);

  useEffect(() => {
    return connectJobsLive({
      onPayload: (payload: JobsLivePayload) => {
        if (payload.table) {
          setJobs(payload.table.jobs);
        }
      },
      onState: setSocketState
    });
  }, []);

  const filteredJobs = useMemo(() => {
    const query = filter.trim().toLowerCase();
    if (!query) {
      return jobs;
    }
    return jobs.filter((job) =>
      [job.id, job.playbook, job.status, jobTarget(job)].some((value) =>
        (value ?? "").toLowerCase().includes(query)
      )
    );
  }, [filter, jobs]);

  const counts = useMemo(() => summarizeJobs(jobs), [jobs]);

  return (
    <PageFrame bootstrap={bootstrap} title="Jobs" section="Observe" path="/react/jobs" socketState={socketState}>
      {error ? <p className="notice" role="status">{error}</p> : null}
      <section className="metric-strip" aria-label="Jobs metrics">
        <Metric label="Total" value={String(counts.total)} />
        <Metric label="Running" value={String(counts.running)} tone={counts.running > 0 ? "active" : "neutral"} />
        <Metric label="Queued" value={String(counts.queued)} tone={counts.queued > 0 ? "active" : "neutral"} />
        <Metric label="Failed" value={String(counts.failed)} tone={counts.failed > 0 ? "bad" : "good"} />
      </section>
      <label className="filter">
        <span>Filter jobs</span>
        <input
          aria-label="Filter jobs"
          value={filter}
          onChange={(event) => {
            setFilter(event.target.value);
          }}
          placeholder="Job id, playbook, target, status"
        />
      </label>
      <Panel title="Jobs table">
        <JobsTable jobs={filteredJobs} />
      </Panel>
    </PageFrame>
  );
}
