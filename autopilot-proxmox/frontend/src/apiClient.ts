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
    const body = (await response.json().catch(() => null)) as { detail?: unknown; message?: unknown; error?: unknown } | null;
    const detail = body?.detail ?? body?.message ?? body?.error;
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
  }
  if (contentType.includes("text/html")) {
    const html = await response.text().catch(() => "");
    const title = /<title[^>]*>([^<]+)<\/title>/iu.exec(html)?.[1]?.trim();
    const heading = /<h1[^>]*>([^<]+)<\/h1>/iu.exec(html)?.[1]?.trim();
    return title || heading || response.statusText || `HTTP ${String(response.status)}`;
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

export async function postJson<T>(path: string, body: Readonly<Record<string, unknown>> = {}): Promise<T> {
  return fetchJson<T>(path, {
    method: "POST",
    headers: {
      "content-type": "application/json"
    },
    body: JSON.stringify(body)
  });
}

export async function postForm<T>(path: string, body: FormData): Promise<T> {
  return fetchJson<T>(path, {
    method: "POST",
    body
  });
}

export async function putJson<T>(path: string, body: Readonly<Record<string, unknown>> = {}): Promise<T> {
  return fetchJson<T>(path, {
    method: "PUT",
    headers: {
      "content-type": "application/json"
    },
    body: JSON.stringify(body)
  });
}

export async function deleteJson<T>(path: string): Promise<T> {
  return fetchJson<T>(path, { method: "DELETE" });
}
