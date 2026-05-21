import { useCallback, useMemo, useRef, useState } from "react";

import { fetchJson, postForm } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, FileShelfRow, FilesResponse } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { bytesLabel, lowerText, textValue } from "../utilityModels";

const FILE_LIMIT_LABEL = "Up to 10 GiB per file. Any file type.";

type UploadQueueStatus = "queued" | "uploading" | "complete" | "failed";

interface UploadQueueItem {
  readonly id: string;
  readonly file: File;
  readonly name: string;
  readonly size: number;
  readonly status: UploadQueueStatus;
  readonly progress: number;
  readonly error?: string;
}

function fileDownloadUrl(row: FileShelfRow): string {
  return row.url || `/files/${encodeURIComponent(row.name)}`;
}

function queueId(file: File, index: number): string {
  return `${file.name}:${String(file.size)}:${String(file.lastModified)}:${String(index)}`;
}

function htmlDetail(text: string): string {
  const title = /<title[^>]*>([^<]+)<\/title>/iu.exec(text)?.[1]?.trim();
  const heading = /<h1[^>]*>([^<]+)<\/h1>/iu.exec(text)?.[1]?.trim();
  return title || heading || text.trim();
}

function xhrDetail(xhr: XMLHttpRequest): string {
  const contentType = xhr.getResponseHeader("content-type") || "";
  const raw = typeof xhr.responseText === "string" ? xhr.responseText : "";
  if (contentType.includes("application/json")) {
    try {
      const body = JSON.parse(raw) as { readonly detail?: unknown; readonly message?: unknown; readonly error?: unknown };
      const detail = body.detail ?? body.message ?? body.error;
      if (typeof detail === "string" && detail.trim()) {
        return detail;
      }
    } catch {
      return xhr.statusText || `HTTP ${String(xhr.status)}`;
    }
  }
  if (contentType.includes("text/html")) {
    return htmlDetail(raw) || xhr.statusText || `HTTP ${String(xhr.status)}`;
  }
  return raw.trim() || xhr.statusText || `HTTP ${String(xhr.status)}`;
}

function uploadFileWithProgress(file: File, onProgress: (progress: number) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const form = new FormData();
    form.append("files", file);
    xhr.open("POST", "/api/files/upload");
    xhr.setRequestHeader("accept", "application/json");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.max(0, Math.min(100, Math.round((event.loaded / event.total) * 100))));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(100);
        resolve();
        return;
      }
      reject(new Error(xhrDetail(xhr)));
    };
    xhr.onerror = () => {
      reject(new Error("Upload failed"));
    };
    xhr.send(form);
  });
}

export function FilesPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const fileInput = useRef<HTMLInputElement | null>(null);
  const replaceInputs = useRef<Record<string, HTMLInputElement | null>>({});
  const [rows, setRows] = useState<readonly FileShelfRow[]>([]);
  const [filter, setFilter] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [pendingAction, setPendingAction] = useState("");
  const [uploadQueue, setUploadQueue] = useState<readonly UploadQueueItem[]>([]);
  const [selectedFiles, setSelectedFiles] = useState<ReadonlySet<string>>(() => new Set<string>());

  const load = useCallback(async () => {
    try {
      const payload = await fetchJson<FilesResponse>("/api/files");
      setRows(payload.files);
      const names = new Set(payload.files.map((row) => row.name));
      setSelectedFiles((current) => new Set([...current].filter((name) => names.has(name))));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load files");
    }
  }, []);

  usePolling(load);

  const query = filter.trim().toLowerCase();
  const filtered = useMemo(() => rows.filter((row) => lowerText(row.name).includes(query)), [query, rows]);
  const totalBytes = rows.reduce((sum, row) => sum + (typeof row.size_bytes === "number" ? row.size_bytes : 0), 0);
  const selectedCount = selectedFiles.size;
  const allFilteredSelected = filtered.length > 0 && filtered.every((row) => selectedFiles.has(row.name));

  const updateQueueItem = (id: string, patch: (item: UploadQueueItem) => UploadQueueItem) => {
    setUploadQueue((current) => current.map((item) => (item.id === id ? patch(item) : item)));
  };

  const selectUploadFiles = () => {
    const files = fileInput.current?.files;
    if (!files?.length) {
      return;
    }
    setUploadQueue(Array.from(files).map((file, index) => ({
      id: queueId(file, index),
      file,
      name: file.name,
      size: file.size,
      status: "queued",
      progress: 0
    })));
    setMessage("");
    setError("");
  };

  const uploadFiles = async () => {
    if (!uploadQueue.length) {
      selectUploadFiles();
      return;
    }
    setPendingAction("upload");
    let uploaded = 0;
    let failed = 0;
    try {
      for (const item of uploadQueue) {
        if (item.status === "complete") {
          uploaded += 1;
          continue;
        }
        updateQueueItem(item.id, (current) => ({
          ...current,
          status: "uploading",
          progress: Math.max(current.progress, 1)
        }));
        try {
          await uploadFileWithProgress(item.file, (progress) => {
            updateQueueItem(item.id, (current) => ({
              ...current,
              progress
            }));
          });
          uploaded += 1;
          updateQueueItem(item.id, (current) => ({
            ...current,
            status: "complete",
            progress: 100
          }));
        } catch (err) {
          failed += 1;
          const failure = err instanceof Error ? err.message : "Upload failed";
          updateQueueItem(item.id, (current) => ({
            ...current,
            status: "failed",
            progress: current.progress,
            error: failure
          }));
        }
      }
      if (failed > 0) {
        setError(`Uploaded ${String(uploaded)} of ${String(uploadQueue.length)} file(s); ${String(failed)} failed`);
      } else {
        setError("");
        setMessage(`Uploaded or replaced ${String(uploaded)} file(s)`);
      }
      if (fileInput.current) {
        fileInput.current.value = "";
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "File upload failed");
    } finally {
      setPendingAction("");
    }
  };

  const clearUploadQueue = () => {
    setUploadQueue([]);
    if (fileInput.current) {
      fileInput.current.value = "";
    }
  };

  const replaceFile = async (name: string) => {
    const input = replaceInputs.current[name];
    const file = input?.files?.[0];
    if (!file) {
      setError(`Choose a replacement file for ${name}`);
      return;
    }
    const form = new FormData();
    form.append("file", file);
    setPendingAction(`replace:${name}`);
    try {
      const result = await postForm<{ readonly replaced?: string }>(`/api/files/${encodeURIComponent(name)}/replace`, form);
      setMessage(`Replaced ${result.replaced ?? name}`);
      setError("");
      input.value = "";
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Replace failed for ${name}`);
    } finally {
      setPendingAction("");
    }
  };

  const deleteFile = async (name: string) => {
    if (!window.confirm(`Delete ${name}?`)) {
      return;
    }
    const form = new FormData();
    form.append("files", name);
    setPendingAction(`delete:${name}`);
    try {
      const result = await postForm<{ readonly deleted?: number }>("/api/files/delete", form);
      setMessage(`Deleted ${String(result.deleted ?? 1)} file(s)`);
      setError("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Delete failed for ${name}`);
    } finally {
      setPendingAction("");
    }
  };

  const toggleFileSelection = (name: string) => {
    setSelectedFiles((current) => {
      const next = new Set(current);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  };

  const toggleAllFiltered = () => {
    setSelectedFiles((current) => {
      const next = new Set(current);
      if (allFilteredSelected) {
        filtered.forEach((row) => {
          next.delete(row.name);
        });
      } else {
        filtered.forEach((row) => {
          next.add(row.name);
        });
      }
      return next;
    });
  };

  const deleteSelectedFiles = async () => {
    const names = [...selectedFiles];
    if (!names.length) {
      return;
    }
    if (!window.confirm(`Delete ${String(names.length)} selected file(s)?`)) {
      return;
    }
    const form = new FormData();
    names.forEach((name) => {
      form.append("files", name);
    });
    setPendingAction("delete:selected");
    try {
      const result = await postForm<{ readonly deleted?: number }>("/api/files/delete", form);
      setMessage(`Deleted ${String(result.deleted ?? names.length)} file(s)`);
      setError("");
      setSelectedFiles(new Set<string>());
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Bulk delete failed");
    } finally {
      setPendingAction("");
    }
  };

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Files"
      section="Fleet"
      path="/react/files"
    >
      {message ? <p className="notice" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="File shelf metrics">
        <Metric label="Files" value={String(rows.length)} />
        <Metric label="Storage" value={bytesLabel(totalBytes)} />
        <Metric label="Visible" value={String(filtered.length)} />
        <Metric label="Limit" value="10 GiB" />
      </section>
      <section className="filter-row" aria-label="File shelf controls">
        <div className="filter-row__top">
          <label className="filter">
            <span>Search files</span>
            <input value={filter} onChange={(event) => { setFilter(event.target.value); }} placeholder="Filename" />
          </label>
          <span className="result-count">{String(filtered.length)} of {String(rows.length)}</span>
        </div>
        <div className="utility-upload-row">
          <input
            ref={fileInput}
            type="file"
            multiple
            aria-label="Upload files"
            onChange={selectUploadFiles}
          />
          <button
            className="utility-button"
            type="button"
            disabled={pendingAction === "upload" || uploadQueue.length === 0}
            onClick={() => { void uploadFiles(); }}
          >
            {uploadQueue.length ? `Start upload (${String(uploadQueue.length)})` : "Upload / Replace files"}
          </button>
          {uploadQueue.length ? (
            <button className="utility-button utility-button--muted" type="button" disabled={pendingAction === "upload"} onClick={clearUploadQueue}>
              Clear queue
            </button>
          ) : null}
          <span className="muted">{FILE_LIMIT_LABEL}</span>
        </div>
      </section>
      {uploadQueue.length ? (
        <Panel title="Upload queue">
          <div className="upload-queue" aria-label="Files upload queue">
            {uploadQueue.map((item) => (
              <div className="upload-queue__row" key={item.id}>
                <div>
                  <strong>{item.name}</strong>
                  <small>{bytesLabel(item.size)}</small>
                </div>
                <span className={`upload-queue__status upload-queue__status--${item.status}`}>{item.status === "complete" ? "Complete" : item.status === "failed" ? "Failed" : item.status === "uploading" ? "Uploading" : "Queued"}</span>
                <progress aria-label={`Upload progress for ${item.name}`} value={item.progress} max={100} />
                {item.error ? <small className="notice-inline notice-inline--bad">{item.error}</small> : null}
              </div>
            ))}
          </div>
        </Panel>
      ) : null}
      <Panel title="Uploaded files">
        <div className="bulk-action-row">
          <label className="file-select-all">
            <input
              type="checkbox"
              aria-label="Select all files"
              checked={allFilteredSelected}
              disabled={!filtered.length}
              onChange={toggleAllFiltered}
            />
            <span>{selectedCount ? `${String(selectedCount)} selected` : "Select files"}</span>
          </label>
          <button
            className="utility-button utility-button--danger"
            type="button"
            disabled={selectedCount === 0 || pendingAction === "delete:selected"}
            onClick={() => { void deleteSelectedFiles(); }}
          >
            Delete selected ({String(selectedCount)})
          </button>
        </div>
        <div className="table-wrap">
          <table className="jobs-table utility-table">
            <thead>
              <tr>
                <th className="file-select-cell" scope="col">Select</th>
                <th scope="col">File</th>
                <th scope="col">URL</th>
                <th scope="col">Size</th>
                <th scope="col">Modified</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr key={row.name}>
                  <td className="file-select-cell">
                    <input
                      type="checkbox"
                      aria-label={`Select ${row.name}`}
                      checked={selectedFiles.has(row.name)}
                      onChange={() => { toggleFileSelection(row.name); }}
                    />
                  </td>
                  <td><a href={fileDownloadUrl(row)}>{row.name}</a></td>
                  <td>
                    <a className="mono-link" href={fileDownloadUrl(row)}>{fileDownloadUrl(row)}</a>
                  </td>
                  <td>{bytesLabel(row.size_bytes)}</td>
                  <td>{textValue(row.modified)}</td>
                  <td>
                    <div className="utility-action-stack">
                      <input
                        ref={(input) => {
                          replaceInputs.current[row.name] = input;
                        }}
                        type="file"
                        aria-label={`Replacement file for ${row.name}`}
                      />
                      <div className="utility-action-row">
                        <button
                          className="utility-button"
                          type="button"
                          disabled={pendingAction === `replace:${row.name}`}
                          onClick={() => { void replaceFile(row.name); }}
                        >
                          Replace
                        </button>
                        <button
                          className="utility-button utility-button--danger"
                          type="button"
                          disabled={pendingAction === `delete:${row.name}`}
                          onClick={() => { void deleteFile(row.name); }}
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!filtered.length ? <p className="empty">No files.</p> : null}
      </Panel>
    </PageFrame>
  );
}
