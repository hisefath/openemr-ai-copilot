/**
 * Narrow FHIR R4 types for exactly the fields this dashboard renders, plus safe accessors.
 *
 * WHY HAND-WRITTEN AND NOT A GENERATED FHIR PACKAGE. The full R4 type set is ~150 resources and several
 * megabytes of types for the six we use. More importantly, generated types describe the SPEC, and OpenEMR
 * populates a subset of it — a field the spec marks optional is, in practice, absent far more often than a
 * newcomer expects. Typing only what we read, with every field explicitly optional, forces each call site to
 * handle the absent case at compile time. That is the single largest thing gained by leaving PHP: in the old
 * dashboard a missing `name[0].family` was a blank space at runtime; here it will not compile unless handled.
 */

export interface Coding { system?: string; code?: string; display?: string }
export interface CodeableConcept { coding?: Coding[]; text?: string }
export interface Reference { reference?: string; display?: string; type?: string }
export interface Period { start?: string; end?: string }

export interface HumanName {
  use?: string; text?: string; family?: string; given?: string[]; prefix?: string[]; suffix?: string[];
}

export interface Identifier {
  use?: string; system?: string; value?: string; type?: CodeableConcept;
}

export interface Patient {
  resourceType: 'Patient';
  id?: string;
  active?: boolean;
  name?: HumanName[];
  gender?: string;
  birthDate?: string;
  identifier?: Identifier[];
}

export interface AllergyIntolerance {
  resourceType: 'AllergyIntolerance';
  id?: string;
  code?: CodeableConcept;
  criticality?: string;
  clinicalStatus?: CodeableConcept;
  verificationStatus?: CodeableConcept;
  reaction?: { manifestation?: CodeableConcept[]; severity?: string }[];
  recordedDate?: string;
}

export interface Condition {
  resourceType: 'Condition';
  id?: string;
  code?: CodeableConcept;
  clinicalStatus?: CodeableConcept;
  verificationStatus?: CodeableConcept;
  onsetDateTime?: string;
  recordedDate?: string;
}

export interface MedicationRequest {
  resourceType: 'MedicationRequest';
  id?: string;
  status?: string;
  intent?: string;
  medicationCodeableConcept?: CodeableConcept;
  medicationReference?: Reference;
  authoredOn?: string;
  requester?: Reference;
  dosageInstruction?: { text?: string }[];
}

export interface CareTeam {
  resourceType: 'CareTeam';
  id?: string;
  status?: string;
  name?: string;
  period?: Period;
  participant?: { member?: Reference; role?: CodeableConcept[] }[];
}

export interface Observation {
  resourceType: 'Observation';
  id?: string;
  status?: string;
  code?: CodeableConcept;
  effectiveDateTime?: string;
  issued?: string;
  valueQuantity?: { value?: number; unit?: string };
  valueString?: string;
  component?: { code?: CodeableConcept; valueQuantity?: { value?: number; unit?: string } }[];
}

export type AnyResource =
  | Patient | AllergyIntolerance | Condition | MedicationRequest | CareTeam | Observation;

/* ------------------------------------------------------------------ accessors
 * Every one of these returns a string that is always safe to render. FHIR's
 * CodeableConcept can carry a display, a text, a bare code, or nothing at all,
 * and OpenEMR produces all four. Centralising that here means no component
 * ever writes `?.coding?.[0]?.display ?? ...` and no component renders "undefined".
 */

export const EM_DASH = '—';

export function codeText(c?: CodeableConcept): string {
  if (!c) return EM_DASH;
  if (c.text?.trim()) return c.text.trim();
  const coded = c.coding?.find((x) => x.display?.trim() || x.code?.trim());
  return coded?.display?.trim() || coded?.code?.trim() || EM_DASH;
}

export function patientName(p?: Patient): string {
  const n = p?.name?.find((x) => x.use === 'official') ?? p?.name?.[0];
  if (!n) return 'Unknown patient';
  if (n.text?.trim()) return n.text.trim();
  const given = (n.given ?? []).filter(Boolean).join(' ');
  const full = [given, n.family].filter(Boolean).join(' ').trim();
  return full || 'Unknown patient';
}

/** OpenEMR exposes the medical record number as an identifier; fall back to the resource id, never to blank. */
export function mrn(p?: Patient): string {
  const withType = p?.identifier?.find((i) =>
    i.type?.coding?.some((c) => c.code === 'MR') || /\bmrn?\b/i.test(i.type?.text ?? ''));
  return withType?.value?.trim() || p?.identifier?.[0]?.value?.trim() || p?.id || EM_DASH;
}

/** Age in whole years, or an em dash. Computed here rather than trusted from the server. */
export function age(birthDate?: string): string {
  if (!birthDate) return EM_DASH;
  const b = new Date(birthDate);
  if (Number.isNaN(b.getTime())) return EM_DASH;
  const now = new Date();
  let y = now.getFullYear() - b.getFullYear();
  const m = now.getMonth() - b.getMonth();
  if (m < 0 || (m === 0 && now.getDate() < b.getDate())) y -= 1;
  return y >= 0 && y < 150 ? `${y}y` : EM_DASH;
}

export function shortDate(iso?: string): string {
  if (!iso) return EM_DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

/** 'active' | 'resolved' | … from a clinicalStatus CodeableConcept. */
export function statusCode(c?: CodeableConcept): string {
  return (c?.coding?.[0]?.code ?? c?.text ?? '').toLowerCase();
}

export function observationValue(o: Observation): string {
  if (o.valueQuantity?.value !== undefined) {
    const u = o.valueQuantity.unit ? ` ${o.valueQuantity.unit}` : '';
    return `${o.valueQuantity.value}${u}`;
  }
  if (o.valueString?.trim()) return o.valueString.trim();
  // Blood pressure and similar arrive as components rather than a single value.
  const parts = (o.component ?? [])
    .map((c) => (c.valueQuantity?.value !== undefined ? String(c.valueQuantity.value) : null))
    .filter((x): x is string => x !== null);
  if (parts.length) {
    const unit = o.component?.find((c) => c.valueQuantity?.unit)?.valueQuantity?.unit ?? '';
    return `${parts.join('/')}${unit ? ` ${unit}` : ''}`;
  }
  return EM_DASH;
}
