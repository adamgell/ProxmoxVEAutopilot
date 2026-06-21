import { useMemo, useState } from "react";

import { cidrHostPrefix, type FieldDef } from "../networksSchema";

interface SdnInlineFormProps {
  readonly mode: "create" | "edit";
  readonly title: string;
  readonly fields: readonly FieldDef[];
  readonly initialValues?: Readonly<Record<string, string>> | undefined;
  readonly submitLabel?: string | undefined;
  readonly busy?: boolean | undefined;
  readonly error?: string | undefined;
  readonly onSubmit: (values: Readonly<Record<string, string>>) => void;
  readonly onCancel: () => void;
}

function asString(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (typeof value === "number" || typeof value === "bigint") {
    return String(value);
  }
  return "";
}

function defaultValuesFor(fields: readonly FieldDef[], initial?: Readonly<Record<string, string>>): Record<string, string> {
  const seed: Record<string, string> = {};
  for (const field of fields) {
    const initialValue = initial?.[field.name];
    if (initialValue !== undefined) {
      seed[field.name] = asString(initialValue);
      continue;
    }
    if (field.kind === "checkbox") {
      seed[field.name] = "false";
    } else if (field.kind === "select" && field.options && field.options.length > 0) {
      const firstOption = field.options[0];
      seed[field.name] = firstOption ? firstOption.value : "";
    } else {
      seed[field.name] = "";
    }
  }
  return seed;
}

export function SdnInlineForm({
  mode,
  title,
  fields,
  initialValues,
  submitLabel,
  busy,
  error,
  onSubmit,
  onCancel
}: SdnInlineFormProps) {
  const [values, setValues] = useState<Record<string, string>>(() => defaultValuesFor(fields, initialValues));

  const visibleFields = useMemo(
    () => fields.filter((field) => !field.showWhen || field.showWhen(values)),
    [fields, values]
  );

  function update(name: string, raw: string) {
    setValues((current) => ({ ...current, [name]: raw }));
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit(values);
  }

  return (
    <form
      className="sdn-inline-form"
      onSubmit={handleSubmit}
      aria-label={title}
    >
      <header className="sdn-inline-form__header">
        <strong>{title}</strong>
        <button type="button" className="sdn-inline-form__cancel" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </header>
      <div className="sdn-inline-form__grid">
        {visibleFields.map((field) => {
          const value = values[field.name] ?? "";
          if (field.kind === "checkbox") {
            return (
              <label key={field.name} className="cloudosd-field sdn-inline-form__checkbox">
                <input
                  type="checkbox"
                  checked={value === "true"}
                  onChange={(event) => {
                    update(field.name, event.currentTarget.checked ? "true" : "false");
                  }}
                  disabled={busy}
                  aria-label={field.label}
                />
                <span>{field.label}</span>
              </label>
            );
          }
          if (field.kind === "select" && field.options) {
            return (
              <label key={field.name} className="cloudosd-field">
                <span>{field.label}{field.required ? " *" : ""}</span>
                <select
                  value={value}
                  onChange={(event) => {
                    update(field.name, event.currentTarget.value);
                  }}
                  disabled={busy}
                  aria-label={field.label}
                >
                  {field.options.map((opt) => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
                {field.help ? <span className="sdn-inline-form__help">{field.help}</span> : null}
              </label>
            );
          }
          const derivedPrefix = field.prefixFrom ? cidrHostPrefix(values[field.prefixFrom]) : "";
          return (
            <label key={field.name} className="cloudosd-field">
              <span>{field.label}{field.required ? " *" : ""}</span>
              {derivedPrefix ? (
                <div className="sdn-inline-form__octet">
                  <span className="sdn-inline-form__octet-prefix">{derivedPrefix}.</span>
                  <input
                    type={field.kind === "number" ? "number" : "text"}
                    inputMode={field.kind === "number" ? "numeric" : undefined}
                    min={field.kind === "number" ? 1 : undefined}
                    max={field.kind === "number" ? 254 : undefined}
                    value={value}
                    placeholder={field.placeholder}
                    onChange={(event) => {
                      update(field.name, event.currentTarget.value);
                    }}
                    required={Boolean(field.required) && mode === "create"}
                    disabled={busy || (mode === "edit" && field.editable === false)}
                    aria-label={field.label}
                  />
                </div>
              ) : (
                <input
                  type={field.kind === "number" ? "number" : "text"}
                  value={value}
                  placeholder={field.placeholder}
                  onChange={(event) => {
                    update(field.name, event.currentTarget.value);
                  }}
                  required={Boolean(field.required) && mode === "create"}
                  disabled={busy || (mode === "edit" && field.editable === false)}
                  aria-label={field.label}
                />
              )}
              {field.help ? <span className="sdn-inline-form__help">{field.help}</span> : null}
            </label>
          );
        })}
      </div>
      {error ? <p className="notice sdn-inline-form__error" role="status">{error}</p> : null}
      <div className="sdn-inline-form__actions">
        <button type="submit" className="utility-button" disabled={busy}>
          {busy ? "Saving..." : (submitLabel ?? (mode === "create" ? "Create" : "Save"))}
        </button>
      </div>
    </form>
  );
}
