export const WINDOWS_COMPUTER_NAME_LIMIT = 15;

export interface ProvisionNaming {
  readonly groupTag: string;
  readonly hostnamePattern: string;
  readonly previewName: string;
  readonly previewLength: number;
  readonly limit: number;
  readonly safe: boolean;
  readonly normalized: string;
}

const INDEX_PREVIEW = "01";
const VMID_PREVIEW = "105";
const SERIAL_PREVIEW = "SERIAL01";
const INDEX_SUFFIX_LENGTH = 1 + INDEX_PREVIEW.length;
const NUMERIC_PREFIX = "pve-";
const FALLBACK_BASE = "ap";

export function deriveProvisionNaming(runTag: string): ProvisionNaming {
  const base = deriveHostnameBase(runTag);
  const hostnamePattern = `${base}-{index}`;
  const preview = previewHostnamePattern(hostnamePattern);

  return {
    groupTag: runTag,
    hostnamePattern,
    ...preview
  };
}

export function previewHostnamePattern(
  pattern: string
): Pick<ProvisionNaming, "previewName" | "previewLength" | "limit" | "safe" | "normalized"> {
  const previewName = pattern
    .replace(/\{index\}/gi, INDEX_PREVIEW)
    .replace(/\{vmid\}/gi, VMID_PREVIEW)
    .replace(/\{serial\}/gi, SERIAL_PREVIEW);
  const normalized = normalizeComputerName(previewName);

  return {
    previewName,
    previewLength: previewName.length,
    limit: WINDOWS_COMPUTER_NAME_LIMIT,
    safe: previewName === normalized,
    normalized
  };
}

export function normalizeHostnameBase(value: string, reservedSuffixLength = 0): string {
  const maxLength = Math.max(1, WINDOWS_COMPUTER_NAME_LIMIT - Math.max(0, reservedSuffixLength));
  const base = normalizeName(value, maxLength);

  if (base.length === 0) {
    return FALLBACK_BASE.slice(0, maxLength);
  }

  if (/^\d+$/.test(base)) {
    const digitLength = Math.max(1, maxLength - NUMERIC_PREFIX.length);
    return normalizeName(`${NUMERIC_PREFIX}${base.slice(0, digitLength)}`, maxLength);
  }

  return base;
}

function deriveHostnameBase(runTag: string): string {
  const tokens = runTag.trim().split(/[^A-Za-z0-9]+/).filter(Boolean);

  if (tokens.length === 0) {
    return FALLBACK_BASE;
  }

  if (tokens.every((token) => /^\d+$/.test(token))) {
    return normalizeHostnameBase(tokens.join(""), INDEX_SUFFIX_LENGTH);
  }

  const tenantToken = tokens[0] ?? FALLBACK_BASE;
  const compactTenant = tenantToken.match(/^([A-Za-z]+)(\d+)$/);

  if (compactTenant && tokens.length > 1) {
    const letters = compactTenant[1] ?? "";
    const digits = compactTenant[2] ?? "";
    return normalizeHostnameBase(`${letters.slice(0, 3)}${digits.slice(-2)}`, INDEX_SUFFIX_LENGTH);
  }

  return normalizeHostnameBase(tenantToken, INDEX_SUFFIX_LENGTH);
}

function normalizeComputerName(value: string): string {
  const normalized = normalizeName(value, WINDOWS_COMPUTER_NAME_LIMIT);

  if (/^\d+$/.test(normalized)) {
    return normalizeHostnameBase(normalized);
  }

  return normalized.length > 0 ? normalized : FALLBACK_BASE;
}

function normalizeName(value: string, maxLength: number): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, maxLength)
    .replace(/-+$/g, "");
}
