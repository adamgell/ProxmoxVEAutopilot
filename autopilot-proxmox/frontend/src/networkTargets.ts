import { textValue } from "./utilityModels";

export interface NetworkTargetOption {
  readonly kind?: string;
  readonly value?: string;
  readonly label?: string;
  readonly zone?: string;
}

interface NetworkTargetOptionsPayload {
  readonly network_targets?: readonly NetworkTargetOption[];
  readonly bridges?: readonly string[];
  readonly defaults?: {
    readonly bridge?: string;
  };
}

export function networkTargetLabel(target: NetworkTargetOption): string {
  const value = textValue(target.value, "");
  const label = textValue(target.label, value);
  const zone = textValue(target.zone, "");
  if (target.kind === "sdn_vnet" && zone) {
    return `${label} (SDN: ${zone})`;
  }
  if (target.kind === "sdn_vnet") {
    return `${label} (SDN)`;
  }
  return label;
}

export function networkTargetOptions(options: NetworkTargetOptionsPayload): readonly { readonly value: string; readonly label: string }[] {
  const targets = (options.network_targets ?? [])
    .map((target) => ({
      value: textValue(target.value, ""),
      label: networkTargetLabel(target)
    }))
    .filter((target) => target.value.length > 0);
  if (targets.length) {
    return targets;
  }
  const bridges = (options.bridges ?? []).filter((value) => value.trim().length > 0);
  const fallback = textValue(options.defaults?.bridge, "");
  const bridgeTargets = bridges.length ? bridges : (fallback ? [fallback] : []);
  return bridgeTargets.map((bridge) => ({ value: bridge, label: bridge }));
}
