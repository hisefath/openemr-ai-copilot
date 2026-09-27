/**
 * The only place this app talks to the network.
 *
 * Every call goes to the agent's backend-for-frontend, never to OpenEMR directly. The browser therefore
 * never sees an access token — it holds an opaque session handle and nothing else. See
 * PATIENT_DASHBOARD_MIGRATION.md: OpenEMR refuses `user/` scopes to public OAuth clients, so a
 * token-in-the-browser SPA is not merely discouraged here, it is rejected at client registration.
 */
import type { AnyResource, Patient } from './fhir';

/** Dev runs against Vite's proxy; the built bundle is served by the agent itself, so same-origin. */
const BASE = import.meta.env.VITE_API_BASE ?? '';

/** The session handle issued by the SMART launch. Read once; never persisted anywhere but memory. */
let sessionHandle: string | null = null;

export function setSession(handle: string) {
  sessionHandle = handle;
}

export function getSession(): string | null {
  return sessionHandle;
}

/**
 * Adopts the session handle the SMART callback injected into the page, then removes it from the DOM.
 *
 * This is the SAME mechanism the Week 1 panel uses (`static/panel.js`), reused rather than reinvented: the
 * handle rides in a `<meta name="copilot-session">` tag that the server substitutes at callback time, so it
 * never appears in the URL, never reaches an access log or a Referer header, and never lands in browser
 * history. The callback's own `?code=&state=` query is stripped for the same reason.
 */
export function adoptSessionFromPage(): string | null {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="copilot-session"]');
  const handle = meta?.content?.trim() || null;
  if (meta) meta.remove();
  if (window.location.search) {
    window.history.replaceState(null, '', window.location.pathname);
  }
  if (handle) setSession(handle);
  return handle;
}

export class ApiError extends Error {
  // Declared as a field rather than a constructor parameter property: TypeScript 6 runs with
  // `erasableSyntaxOnly`, which rejects any syntax that emits runtime code from a type position.
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function get<T>(path: string): Promise<T> {
  if (!sessionHandle) throw new ApiError('no_session', 401);
  const res = await fetch(`${BASE}/api/dashboard${path}`, {
    headers: { Authorization: `Bearer ${sessionHandle}` },
  });
  if (!res.ok) {
    // The BFF returns non-PHI reason codes, so the body is safe to surface to the UI verbatim.
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* a non-JSON error body is still just a status to us */
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

export type CardKey =
  | 'allergies' | 'problems' | 'medications' | 'prescriptions' | 'careteam' | 'vitals';

/**
 * `status` mirrors the agent's LoadStatus. `empty` is a FACT — the chart records none of this — and is
 * rendered differently from a failure. That distinction is carried over deliberately from the Week 2
 * extraction work: "none recorded" and "we could not find out" must never look the same.
 */
export interface CardResponse<T extends AnyResource = AnyResource> {
  card: CardKey;
  status: 'ok' | 'empty' | 'pending' | 'forbidden' | 'expired' | 'error' | string;
  resources: T[];
}

export const fetchPatient = () => get<Patient>('/patient');

export const fetchCard = <T extends AnyResource>(key: CardKey) =>
  get<CardResponse<T>>(`/cards/${key}`);
