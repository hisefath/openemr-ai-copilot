# PATIENT_DASHBOARD_MIGRATION.md

Porting OpenEMR's patient dashboard from server-rendered PHP to a typed client application, consuming the
existing REST and FHIR API as the data layer. No backend was modified. No interface was redesigned.

**What shipped:** `dashboard/` — React 19 + TypeScript 6 + Vite 8, 80.9 kB gzipped, served by the agent at
`/dashboard/launch`. Authentication is SMART on FHIR (OAuth2 / OpenID Connect). The identity bar plus six
cards — Allergies, Problem List, Medications, Prescriptions, Care Team, and Vitals as the additional
section — each reading live from FHIR R4.

---

## 1. The constraint that chose the architecture

The instinct for "port a page to a modern framework" is a single-page app that holds its own OAuth token and
calls the FHIR API directly from the browser. **OpenEMR refuses to register that client.**

```php
// src/RestControllers/AuthorizationController.php:324-329
// don't allow user, system scopes, and offline_access for public apps
} elseif (
    str_contains($scope, 'system/')
    || str_contains($scope, 'user/')
) {
    throw new OAuthServerException(
        "system and user scopes are only allowed for confidential clients", 0, 'invalid_client_metadata');
}
```

A clinician-facing dashboard needs `user/` scopes — `user/Patient.rs`, `user/AllergyIntolerance.rs` and the
rest. A browser app cannot keep a client secret, so it must register as a *public* client, and OpenEMR
rejects a public client that asks for `user/` scopes. The alternative, `patient/` scopes, is the
patient-portal persona and returns a different and narrower view.

So the architecture is not a preference. **A browser-only dashboard cannot be built against this API at
all.** The access token has to live on a server.

That turns out to agree with where the industry has landed. The IETF's current best practice for
browser-based apps recommends a backend-for-frontend over storing tokens in the browser, for exactly the
reason OpenEMR enforces: a token in JavaScript is a token exposed to every script on the page. Being pushed
into the recommended pattern by a server-side check is a good outcome.

**Decision: a backend-for-frontend (BFF).** The browser holds an opaque session handle. The token never
leaves the server.

### Why the agent is the BFF rather than a new service

The Week 1/2 agent already implements this exact flow and has for weeks: `smart.py` does the SMART launch
with PKCE, `sessions.py` holds tokens server-side, `fhir.py` fetches and validates FHIR with a patient lock.
That path is covered by a 55-case eval gate, and its one real defect — six write scopes admitted on return
but never requested — was caught by CI rather than in production.

Writing a second authentication path would have meant a second thing to get wrong. The BFF is 95 lines
(`agent/copilot/dashboard_routes.py`) and adds no auth code at all.

- **Pro:** zero new auth surface; reuses the session, patient lock and audit trail already under test.
- **Pro:** the browser cannot be made to leak a token it never receives.
- **Con:** the dashboard now depends on the agent being up. Before, the PHP page died only when OpenEMR did.
- **Con:** one more network hop per card — measured below, and it is not the dominant cost.

---

## 2. Framework choice: React + TypeScript + Vite

### Why a client app at all, rather than modern server rendering

The brief fixes the data layer as "OpenEMR's existing REST and FHIR API". Everything the dashboard renders
is already a JSON endpoint. A server-rendering framework (Next.js, Nuxt, or PHP with a nicer template
engine) would add a rendering tier whose only job is to fetch that JSON and turn it into HTML — a second
server to deploy, and a second place for state to be wrong.

This screen is also behind authentication, so the usual arguments for SSR do not apply: there is no SEO, no
social preview, and no anonymous first paint to optimise.

### Why TypeScript is the actual win, not React

If one sentence has to justify leaving PHP, it is this: **FHIR is a deeply optional data format, and PHP had
no way to make that visible.**

Almost every field in a FHIR resource is optional by specification, and OpenEMR populates a subset of the
spec. A patient's name is `name[0].family` — where `name` may be absent, may be an array of several with
different `use` values, and may carry only a `text`. In the PHP dashboard, a missing name is a blank space
at runtime, discovered by a user.

```ts
// dashboard/src/fhir.ts
export function patientName(p?: Patient): string {
  const n = p?.name?.find((x) => x.use === 'official') ?? p?.name?.[0];
  if (!n) return 'Unknown patient';
  ...
}
```

Every field in `fhir.ts` is declared optional, so **the compiler refuses to build code that ignores the
absent case.** The class of bug that produces a blank field or `undefined` on a clinical screen is now a
build error. That is a categorical improvement over "be careful", and it is the single strongest thing
gained by the move.

Types are hand-written for the six resources actually rendered rather than generated from the full R4 spec
(~150 resources). Generated types describe what the *specification* permits; hand-written ones describe what
*this server* returns, and they stay small enough to read.

### Why React specifically, and what it buys that Vue or Svelte would not

Honestly: less than the TypeScript decision. Vue or Svelte would both work, and Svelte would ship a smaller
bundle. React was chosen for two concrete reasons rather than popularity.

**TanStack Query.** The dashboard is six independent asynchronous reads. That is not a UI problem, it is a
server-state problem: caching, staleness, retry, and per-card loading and error states. TanStack Query is
the most mature answer to it, and it is what makes the central improvement possible:

```tsx
// dashboard/src/Card.tsx — each card owns its own request
const q = useQuery({ queryKey: ['card', cardKey], queryFn: () => fetchCard<T>(cardKey), staleTime: 30_000 });
```

The PHP dashboard rendered every panel inside one server-side pass. The slowest query set the latency of the
entire page, and one failing query could empty the screen. Here, **a slow Care Team is a slow Care Team** —
the other five cards are already rendered and interactive. `test_a_failing_card_reports_its_own_status_
instead_of_500ing_the_page` asserts exactly that: Care Team returns HTTP 500 upstream and the route still
answers 200 with a status the UI renders as "unavailable".

**Hiring and handover.** This is a fork of a twenty-five-year-old open-source project that outlives whoever
touches it. React is the option most likely to have a maintainer in five years. That is a real engineering
criterion, not a fashionable one.

### Tradeoffs accepted, stated plainly

| Gained | Cost |
|---|---|
| Absent-field bugs become compile errors | A build step where PHP had none; `dist/` must be built and shipped |
| Cards load and fail independently | 80.9 kB of JavaScript where the PHP page shipped ~0 |
| Client-side cache, no full reload to refresh one card | A second runtime and skill set in an otherwise PHP codebase |
| Testable without a browser or a database | Two deploy artifacts instead of one |

The bundle is the honest cost. 80.9 kB gzipped is small for a React app and infinitely more than zero. For a
screen a clinician opens dozens of times a day on a warm cache it is the right trade; for a rarely-visited
page it would not be.

---

## 3. Feature parity, and one deliberate divergence

| Requirement | Source | Status |
|---|---|---|
| Authentication (OAuth2 / OIDC) | `smart.py`, reused unchanged | ✅ |
| Patient header — name, DOB, sex, MRN, active | `Patient` | ✅ |
| Allergies | `AllergyIntolerance` | ✅ |
| Problem List | `Condition` | ✅ |
| Medications | `MedicationRequest` | ✅ |
| Prescriptions | `MedicationRequest?intent=order` | ✅ |
| Care Team | `CareTeam` | ✅ |
| **Additional section** | `Observation?category=vital-signs` | ✅ Vitals |

**Why Vitals for the free choice.** It is the only card whose payload has a different *shape*: the other
five are code-plus-status, while an Observation carries a `valueQuantity`, or a `valueString`, or — for
blood pressure — a `component` array with two values and one unit. Choosing it forced the rendering layer to
handle a genuinely different response rather than a sixth variation of the same one.

**The divergence: empty is not the same as unavailable.** OpenEMR's dashboard shows an empty panel whether
the chart records nothing or the query failed. This dashboard separates them, because they are different
facts and a clinician acts differently on each:

- `status: "empty"` → *"No known allergies recorded."*
- `status: "error"` → *"Could not load allergies"* with the reason and a Retry button
- `status: "forbidden"` → *"Your role does not permit viewing allergies."*

This is the same principle the Week 2 document extraction runs on, where a value that cannot be located
keeps its value and loses its bounding box. **A gap you know about is safe; a gap you invented over is not.**
It is more information than the original UI, not a redesign of it.

---

## 4. Security properties

- **No token in the browser.** Enforced by OpenEMR's public-client rule, not by convention.
- **The session handle never enters the URL.** It is substituted into a `<meta name="copilot-session">` tag
  at callback time; the script reads it into memory and removes the element. Same mechanism as the Week 1
  panel, reused rather than reinvented, so it never reaches an access log, a `Referer` header, or history.
- **Read-only, mechanically.** `test_dashboard_routes_never_write` asserts every route on the BFF exposes
  only `GET`/`HEAD`. The brief says presentation layer; a test enforces it rather than a comment promising it.
- **Patient lock preserved.** `fhir.py` already records a cross-patient response as an error rather than
  rendering it. Writing the tests surfaced this: fixtures without a patient reference came back `error`, as
  they should.

---

## 5. What I would do next

- **Virtualise long lists.** A patient with 200 problems renders 200 DOM nodes today.
- **Trend the vitals.** The data is a time series and it is rendered as a list, which wastes it.
- **A shared `useCard` error boundary** so a rendering exception in one card cannot blank the grid — the BFF
  isolates *fetch* failures per card, but a render throw is still shared.
- **Measure against the PHP page.** I have the client-side numbers and no comparison, so "faster than PHP"
  is not a claim I will make.

---

## 6. Verification

```
408 tests pass                 (9 new, covering the BFF)
tsc --noEmit                   clean under erasableSyntaxOnly
vite build                     262 kB raw / 80.9 kB gzipped
```

The nine BFF tests each name the failure they guard: a header that renders blank because a read bundle was
not unwrapped, a card that 500s the page, an empty card that looks like a failure, a card key the UI
requests that the BFF does not serve, and a build that silently drops the session placeholder.
