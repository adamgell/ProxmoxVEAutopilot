export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function responseDetail(response: Response): Promise<string> {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const body = (await response.json().catch(() => null)) as { detail?: unknown; message?: unknown } | null;
    const detail = body?.detail ?? body?.message;
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
  }
  const text = await response.text().catch(() => "");
  return text.trim() || response.statusText || `HTTP ${String(response.status)}`;
}

export async function fetchJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = init.method || "GET";
  const headers = new Headers(init.headers);
  if (!headers.has("accept")) {
    headers.set("accept", "application/json");
  }
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers
  });

  if (!response.ok) {
    throw new ApiError(`${method} ${path} failed: ${await responseDetail(response)}`, response.status);
  }

  return (await response.json()) as T;
}
