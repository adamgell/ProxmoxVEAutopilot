/**
 * Field/schema definitions for SDN object CRUD in NetworksPage.
 *
 * Each "kind" has a `fields` array describing the controls the inline
 * create/edit forms render, plus a `bodyFor(values)` that maps the form
 * state into the JSON body the backend POST/PATCH endpoints expect.
 *
 * Keep this file pure (no React) so it can be unit-tested in isolation.
 */

export type FieldKind = "text" | "number" | "select" | "checkbox";

export interface FieldDef {
  readonly name: string;
  readonly label: string;
  readonly kind: FieldKind;
  readonly required?: boolean;
  readonly placeholder?: string;
  readonly help?: string;
  /** When set, the input is rendered as a select with these options. */
  readonly options?: readonly { readonly value: string; readonly label: string }[];
  /** When set, only render the field if predicate(values) returns true. */
  readonly showWhen?: (values: Readonly<Record<string, string>>) => boolean;
  /** When false, the field is omitted from edit forms (immutable id, etc). */
  readonly editable?: boolean;
  /**
   * Name of another field whose value supplies a derived prefix. Used by
   * the CIDR-octet inputs on the Subnet form: setting prefixFrom: "subnet"
   * renders the first three octets of values["subnet"] as a static label
   * before the input, so the operator only types the last octet.
   */
  readonly prefixFrom?: string;
  /**
   * Marks a field as a synthetic helper that doesn't map directly to the
   * PVE API. bodyFromValues skips it so post-processing in the page layer
   * is responsible for combining it into a real PVE field
   * (e.g. dhcp_range_start + dhcp_range_end -> "dhcp-range").
   */
  readonly synthetic?: boolean;
}

export interface SdnKindSchema {
  readonly key: SdnKindKey;
  readonly singular: string;
  readonly plural: string;
  readonly createFields: readonly FieldDef[];
  readonly editFields: readonly FieldDef[];
  readonly createPath: (values: Readonly<Record<string, string>>) => string;
  readonly editPath: (id: string, parent?: string) => string;
  readonly deletePath: (id: string, parent?: string) => string;
  readonly idField: string;
  /**
   * Fields the PVE API requires that aren't editable by the operator.
   * Merged into the body before POST/PATCH (e.g. type: "subnet").
   */
  readonly defaultBody?: Readonly<Record<string, unknown>>;
}

export type SdnKindKey = "zone" | "vnet" | "subnet" | "controller" | "ipam" | "dns";

const zoneTypeOptions = [
  { value: "simple", label: "simple" },
  { value: "vlan", label: "vlan" },
  { value: "qinq", label: "qinq" },
  { value: "vxlan", label: "vxlan" },
  { value: "evpn", label: "evpn" }
];

export const zoneSchema: SdnKindSchema = {
  key: "zone",
  singular: "Zone",
  plural: "Zones",
  idField: "zone",
  createFields: [
    { name: "zone", label: "Zone ID", kind: "text", required: true, placeholder: "e.g. labz1", help: "Lowercase, 8 chars max" },
    { name: "type", label: "Type", kind: "select", required: true, options: zoneTypeOptions },
    { name: "bridge", label: "Bridge", kind: "text", placeholder: "vmbr0", showWhen: (v) => v.type === "vlan" || v.type === "qinq" },
    { name: "tag", label: "Outer VLAN tag", kind: "number", showWhen: (v) => v.type === "qinq" },
    { name: "peers", label: "VXLAN peers", kind: "text", placeholder: "ip,ip", showWhen: (v) => v.type === "vxlan" },
    { name: "controller", label: "EVPN controller", kind: "text", placeholder: "controller id", showWhen: (v) => v.type === "evpn" },
    { name: "vrf-vxlan", label: "VRF VXLAN tag", kind: "number", showWhen: (v) => v.type === "evpn" }
  ],
  editFields: [
    { name: "bridge", label: "Bridge", kind: "text" },
    { name: "tag", label: "Outer VLAN tag", kind: "number" },
    { name: "peers", label: "VXLAN peers", kind: "text" },
    { name: "controller", label: "EVPN controller", kind: "text" },
    { name: "vrf-vxlan", label: "VRF VXLAN tag", kind: "number" }
  ],
  createPath: () => "/api/sdn/zones",
  editPath: (id) => `/api/sdn/zones/${encodeURIComponent(id)}`,
  deletePath: (id) => `/api/sdn/zones/${encodeURIComponent(id)}`
};

export const vnetSchema: SdnKindSchema = {
  key: "vnet",
  singular: "VNet",
  plural: "VNets",
  idField: "vnet",
  createFields: [
    { name: "vnet", label: "VNet ID", kind: "text", required: true, placeholder: "e.g. labv1", help: "Lowercase, 8 chars max" },
    { name: "zone", label: "Zone", kind: "text", required: true, placeholder: "zone id" },
    { name: "tag", label: "VLAN/VXLAN tag", kind: "number", placeholder: "optional" },
    { name: "alias", label: "Alias", kind: "text", placeholder: "human-readable label" },
    { name: "vlanaware", label: "VLAN-aware (Q-in-Q)", kind: "checkbox" }
  ],
  editFields: [
    { name: "zone", label: "Zone", kind: "text" },
    { name: "tag", label: "VLAN/VXLAN tag", kind: "number" },
    { name: "alias", label: "Alias", kind: "text" },
    { name: "vlanaware", label: "VLAN-aware", kind: "checkbox" }
  ],
  createPath: () => "/api/sdn/vnets",
  editPath: (id) => `/api/sdn/vnets/${encodeURIComponent(id)}`,
  deletePath: (id) => `/api/sdn/vnets/${encodeURIComponent(id)}`
};

export const subnetSchema: SdnKindSchema = {
  key: "subnet",
  singular: "Subnet",
  plural: "Subnets",
  idField: "subnet",
  // PVE's /cluster/sdn/vnets/{vnet}/subnets POST requires type=subnet.
  // Keeping it out of createFields so the operator can't change it but
  // injecting it from defaultBody.
  defaultBody: { type: "subnet" },
  createFields: [
    { name: "subnet", label: "CIDR", kind: "text", required: true, placeholder: "10.60.10.0/24" },
    { name: "gateway", label: "Gateway", kind: "text", placeholder: "10.60.10.1" },
    { name: "snat", label: "SNAT enabled", kind: "checkbox" },
    { name: "dnszoneprefix", label: "DNS zone prefix", kind: "text", placeholder: "optional" },
    { name: "dhcp-dns-server", label: "DHCP DNS server", kind: "text" },
    {
      name: "dhcp_range_start",
      label: "DHCP range start (last octet)",
      kind: "number",
      placeholder: "100",
      prefixFrom: "subnet",
      synthetic: true,
      help: "Last octet only; first three come from the CIDR."
    },
    {
      name: "dhcp_range_end",
      label: "DHCP range end (last octet)",
      kind: "number",
      placeholder: "199",
      prefixFrom: "subnet",
      synthetic: true
    }
  ],
  editFields: [
    { name: "gateway", label: "Gateway", kind: "text" },
    { name: "snat", label: "SNAT enabled", kind: "checkbox" },
    { name: "dnszoneprefix", label: "DNS zone prefix", kind: "text" },
    { name: "dhcp-dns-server", label: "DHCP DNS server", kind: "text" },
    {
      name: "dhcp_range_start",
      label: "DHCP range start (last octet)",
      kind: "number",
      // On edit the row's `subnet` field is the PVE URL id
      // (e.g. "labz1-192.168.16.0-24"); the actual CIDR lives in
      // `cidr`, which is what cidrHostPrefix can parse.
      prefixFrom: "cidr",
      synthetic: true
    },
    {
      name: "dhcp_range_end",
      label: "DHCP range end (last octet)",
      kind: "number",
      prefixFrom: "cidr",
      synthetic: true
    }
  ],
  createPath: (values) => `/api/sdn/vnets/${encodeURIComponent(values.parentVnet ?? "")}/subnets`,
  editPath: (id, parent) => `/api/sdn/vnets/${encodeURIComponent(parent ?? "")}/subnets/${encodeURIComponent(id)}`,
  deletePath: (id, parent) => `/api/sdn/vnets/${encodeURIComponent(parent ?? "")}/subnets/${encodeURIComponent(id)}`
};

export const controllerSchema: SdnKindSchema = {
  key: "controller",
  singular: "Controller",
  plural: "Controllers",
  idField: "controller",
  createFields: [
    { name: "controller", label: "Controller ID", kind: "text", required: true, placeholder: "e.g. evpn1" },
    { name: "type", label: "Type", kind: "select", required: true, options: [
      { value: "evpn", label: "evpn" },
      { value: "bgp", label: "bgp" },
      { value: "isis", label: "isis" }
    ] },
    { name: "asn", label: "ASN", kind: "number", placeholder: "65000", showWhen: (v) => v.type === "evpn" || v.type === "bgp" },
    { name: "peers", label: "Peers (comma-separated)", kind: "text", placeholder: "10.0.0.1,10.0.0.2", showWhen: (v) => v.type === "evpn" || v.type === "bgp" },
    { name: "node", label: "Node", kind: "text", showWhen: (v) => v.type === "bgp" || v.type === "isis" }
  ],
  editFields: [
    { name: "asn", label: "ASN", kind: "number" },
    { name: "peers", label: "Peers", kind: "text" },
    { name: "node", label: "Node", kind: "text" }
  ],
  createPath: () => "/api/sdn/controllers",
  editPath: (id) => `/api/sdn/controllers/${encodeURIComponent(id)}`,
  deletePath: (id) => `/api/sdn/controllers/${encodeURIComponent(id)}`
};

export const ipamSchema: SdnKindSchema = {
  key: "ipam",
  singular: "IPAM",
  plural: "IPAM",
  idField: "ipam",
  createFields: [
    { name: "ipam", label: "IPAM ID", kind: "text", required: true, placeholder: "e.g. netbox1" },
    { name: "type", label: "Type", kind: "select", required: true, options: [
      { value: "pve", label: "pve (built-in)" },
      { value: "netbox", label: "netbox" },
      { value: "phpipam", label: "phpipam" }
    ] },
    { name: "url", label: "URL", kind: "text", placeholder: "https://...", showWhen: (v) => v.type === "netbox" || v.type === "phpipam" },
    { name: "token", label: "API token", kind: "text", showWhen: (v) => v.type === "netbox" || v.type === "phpipam" },
    { name: "section", label: "Section", kind: "text", placeholder: "phpipam section id", showWhen: (v) => v.type === "phpipam" }
  ],
  editFields: [],
  createPath: () => "/api/sdn/ipams",
  editPath: (id) => `/api/sdn/ipams/${encodeURIComponent(id)}`,
  deletePath: (id) => `/api/sdn/ipams/${encodeURIComponent(id)}`
};

export const dnsSchema: SdnKindSchema = {
  key: "dns",
  singular: "DNS",
  plural: "DNS",
  idField: "dns",
  createFields: [
    { name: "dns", label: "DNS ID", kind: "text", required: true, placeholder: "e.g. pdns1" },
    { name: "type", label: "Type", kind: "select", required: true, options: [
      { value: "powerdns", label: "powerdns" }
    ] },
    { name: "url", label: "URL", kind: "text", required: true, placeholder: "https://..." },
    { name: "key", label: "API key", kind: "text", required: true },
    { name: "ttl", label: "TTL", kind: "number", placeholder: "300" }
  ],
  editFields: [],
  createPath: () => "/api/sdn/dns",
  editPath: (id) => `/api/sdn/dns/${encodeURIComponent(id)}`,
  deletePath: (id) => `/api/sdn/dns/${encodeURIComponent(id)}`
};

export const sdnSchemas: Readonly<Record<SdnKindKey, SdnKindSchema>> = {
  zone: zoneSchema,
  vnet: vnetSchema,
  subnet: subnetSchema,
  controller: controllerSchema,
  ipam: ipamSchema,
  dns: dnsSchema
};

/**
 * Map raw form values (all strings or "true"/"false") into the JSON body
 * the backend expects. Drops empty / unset fields so PATCH calls don't
 * accidentally clear unrelated config.
 */
/**
 * Return the first three octets of a CIDR address, e.g.
 * "192.168.55.0/24" -> "192.168.55". Returns "" when the value doesn't
 * look like a dotted-quad IPv4 address.
 */
export function cidrHostPrefix(cidr: string | undefined): string {
  if (!cidr) {
    return "";
  }
  const trimmed = cidr.trim();
  if (!trimmed) {
    return "";
  }
  const addr = trimmed.split("/", 1)[0] ?? trimmed;
  const octets = addr.split(".");
  if (octets.length < 4) {
    return "";
  }
  const [a, b, c] = octets;
  if (!a || !b || !c) {
    return "";
  }
  return `${a}.${b}.${c}`;
}

export function bodyFromValues(
  fields: readonly FieldDef[],
  values: Readonly<Record<string, string>>,
  options: { readonly includeEmptyBooleans?: boolean } = {}
): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const field of fields) {
    if (field.showWhen && !field.showWhen(values)) {
      continue;
    }
    if (field.synthetic) {
      continue;
    }
    const raw = values[field.name];
    if (raw === undefined) {
      continue;
    }
    if (field.kind === "checkbox") {
      if (raw === "" && !options.includeEmptyBooleans) {
        continue;
      }
      body[field.name] = raw === "true";
      continue;
    }
    if (field.kind === "number") {
      const trimmed = raw.trim();
      if (trimmed === "") {
        continue;
      }
      const parsed = Number(trimmed);
      if (!Number.isFinite(parsed)) {
        continue;
      }
      body[field.name] = parsed;
      continue;
    }
    const trimmed = raw.trim();
    if (trimmed === "") {
      continue;
    }
    body[field.name] = trimmed;
  }
  return body;
}
