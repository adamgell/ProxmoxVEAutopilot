import { useCallback, useMemo, useRef, useState } from "react";

import { fetchJson, postForm } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, FileShelfRow, FilesResponse } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { bytesLabel, lowerText, textValue } from "../utilityModels";

export function FilesPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const fileInput = useRef<HTMLInputElement | null>(null);
  const [rows, setRows] = useState<readonly FileShelfRow[]>([]);
  const [filter, setFilter] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const payload = await fetchJson<FilesResponse>("/api/files");
      setRows(payload.files);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load files");
    }
  }, []);

  usePolling(load);

  const query = filter.trim().toLowerCase();
  const filtered = useMemo(() => rows.filter((row) => lowerText(row.filename).includes(query)), [query, rows]);
  const totalBytes = rows.reduce((sum, row) => sum + (typeof row.size === "number" ? row.size : 0), 0);

  const uploadFiles = async () => {
    const files = fileInput.current?.files;
    if (!files?.length) {
      return;
    }
    const form = new FormData();
    Array.from(files).forEach((file) => {
      form.append("files", file);
    });
    try {
      const result = await postForm<{ readonly uploaded?: number }>("/api/files/upload", form);
      setMessage(`Uploaded ${String(result.uploaded ?? files.length)} MSI file(s)`);
      if (fileInput.current) {
        fileInput.current.value = "";
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "File upload failed");
    }
  };

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Files"
      section="Fleet"
      path="/react/files"
      action={<a className="action-link" href="/legacy/files">Legacy</a>}
    >
      {message ? <p className="notice" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="File shelf metrics">
        <Metric label="Files" value={String(rows.length)} />
        <Metric label="Storage" value={bytesLabel(totalBytes)} />
        <Metric label="Visible" value={String(filtered.length)} />
        <Metric label="Type" value="MSI" />
      </section>
      <section className="filter-row" aria-label="File shelf controls">
        <div className="filter-row__top">
          <label className="filter">
            <span>Search files</span>
            <input value={filter} onChange={(event) => { setFilter(event.target.value); }} placeholder="MSI filename" />
          </label>
          <span className="result-count">{String(filtered.length)} of {String(rows.length)}</span>
        </div>
        <div className="utility-upload-row">
          <input ref={fileInput} type="file" accept=".msi,application/octet-stream" multiple aria-label="Upload MSI files" />
          <button className="utility-button" type="button" onClick={() => { void uploadFiles(); }}>Upload MSI</button>
        </div>
      </section>
      <Panel title="MSI files">
        <div className="table-wrap">
          <table className="jobs-table utility-table">
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">Size</th>
                <th scope="col">Modified</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr key={row.filename}>
                  <td><a href={`/files/${encodeURIComponent(row.filename)}`}>{row.filename}</a></td>
                  <td>{bytesLabel(row.size)}</td>
                  <td>{textValue(row.mtime)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!filtered.length ? <p className="empty">No MSI files.</p> : null}
      </Panel>
    </PageFrame>
  );
}
