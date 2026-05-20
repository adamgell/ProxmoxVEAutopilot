export function textValue(value: unknown, fallback = "-"): string {
  if (value === null || value === undefined || value === "") {
    return fallback;
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return fallback;
}

export function lowerText(value: unknown): string {
  return textValue(value, "").toLowerCase();
}

export function bytesLabel(value: unknown): string {
  const bytes = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "-";
  }
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  if (bytes >= 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${String(bytes)} B`;
}

export function shortTypeLabel(type: string): string {
  return type.replaceAll("_", " ");
}

export function secretState(isSet: boolean | undefined): string {
  return isSet ? "Set" : "Not set";
}
