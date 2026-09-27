/**
 * The patient dashboard: identity bar plus six independently-loading cards.
 *
 * Feature parity target is OpenEMR's PHP patient dashboard — the same information, the same density,
 * reached through the same REST/FHIR API. Nothing here writes; this is a presentation layer.
 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { adoptSessionFromPage, fetchPatient, getSession } from './api';
import { Card } from './Card';
import {
  age, codeText, EM_DASH, mrn, observationValue, patientName, shortDate, statusCode,
  type AllergyIntolerance, type CareTeam, type Condition, type MedicationRequest, type Observation,
} from './fhir';

export default function App() {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    adoptSessionFromPage();
    setReady(true);
  }, []);

  if (!ready) return null;
  if (!getSession()) return <Launch />;

  return (
    <div className="app">
      <PatientHeader />
      <main className="grid">
        <Card<AllergyIntolerance>
          cardKey="allergies" title="Allergies"
          emptyLabel="No known allergies recorded."
          row={(a) => (
            <>
              <span className="primary">{codeText(a.code)}</span>
              <span className="meta">
                {a.criticality && <Tag tone={a.criticality === 'high' ? 'fail' : 'warn'}>{a.criticality}</Tag>}
                {a.reaction?.[0]?.manifestation?.[0] && (
                  <span>{codeText(a.reaction[0].manifestation[0])}</span>
                )}
              </span>
            </>
          )}
        />

        <Card<Condition>
          cardKey="problems" title="Problem List"
          row={(c) => (
            <>
              <span className="primary">{codeText(c.code)}</span>
              <span className="meta">
                {statusCode(c.clinicalStatus) && <Tag>{statusCode(c.clinicalStatus)}</Tag>}
                <span>{shortDate(c.onsetDateTime ?? c.recordedDate)}</span>
              </span>
            </>
          )}
        />

        <Card<MedicationRequest>
          cardKey="medications" title="Medications"
          row={(m) => (
            <>
              <span className="primary">
                {codeText(m.medicationCodeableConcept) !== EM_DASH
                  ? codeText(m.medicationCodeableConcept)
                  : (m.medicationReference?.display ?? EM_DASH)}
              </span>
              <span className="meta">
                {m.status && <Tag tone={m.status === 'active' ? 'ok' : undefined}>{m.status}</Tag>}
                {m.dosageInstruction?.[0]?.text && <span>{m.dosageInstruction[0].text}</span>}
              </span>
            </>
          )}
        />

        <Card<MedicationRequest>
          cardKey="prescriptions" title="Prescriptions"
          emptyLabel="No prescriptions recorded."
          row={(m) => (
            <>
              <span className="primary">
                {codeText(m.medicationCodeableConcept) !== EM_DASH
                  ? codeText(m.medicationCodeableConcept)
                  : (m.medicationReference?.display ?? EM_DASH)}
              </span>
              <span className="meta">
                <span>{shortDate(m.authoredOn)}</span>
                {m.requester?.display && <span>{m.requester.display}</span>}
              </span>
            </>
          )}
        />

        <Card<CareTeam>
          cardKey="careteam" title="Care Team"
          emptyLabel="No care team recorded."
          row={(t) =>
            (t.participant?.length ? (
              <>
                <span className="primary">{t.name ?? 'Care team'}</span>
                <span className="meta">
                  {t.participant.slice(0, 4).map((p, i) => (
                    <span key={i}>
                      {p.member?.display ?? EM_DASH}
                      {p.role?.[0] ? ` · ${codeText(p.role[0])}` : ''}
                    </span>
                  ))}
                </span>
              </>
            ) : (
              <span className="primary">{t.name ?? 'Care team'}</span>
            ))
          }
        />

        {/* The "one additional section of your choice". Vitals, because it is the only card whose value
            is a number over time — it exercises a different shape of FHIR response (valueQuantity and
            multi-component blood pressure) than the five code-and-status cards above. */}
        <Card<Observation>
          cardKey="vitals" title="Vitals"
          emptyLabel="No vitals recorded."
          row={(o) => (
            <>
              <span className="primary">{codeText(o.code)}</span>
              <span className="meta">
                <span className="value">{observationValue(o)}</span>
                <span>{shortDate(o.effectiveDateTime ?? o.issued)}</span>
              </span>
            </>
          )}
        />
      </main>
      <footer className="foot">
        Presentation layer only · data from OpenEMR FHIR R4 · no writes from this app
      </footer>
    </div>
  );
}

function PatientHeader() {
  const q = useQuery({ queryKey: ['patient'], queryFn: fetchPatient, staleTime: 5 * 60_000 });

  if (q.isPending) return <header className="pt-head loading"><div className="skeleton bar" /></header>;
  if (q.isError) {
    return (
      <header className="pt-head fail">
        <strong>Patient unavailable</strong>
        <span className="reason">{q.error.message}</span>
      </header>
    );
  }

  const p = q.data!;
  const inactive = p.active === false;
  return (
    <header className={`pt-head${inactive ? ' inactive' : ''}`}>
      <div className="identity">
        <h1>{patientName(p)}</h1>
        {inactive && <Tag tone="fail">inactive</Tag>}
      </div>
      <dl className="facts">
        <Fact label="MRN" value={mrn(p)} mono />
        <Fact label="DOB" value={`${shortDate(p.birthDate)} (${age(p.birthDate)})`} />
        <Fact label="Sex" value={p.gender ?? EM_DASH} />
        <Fact label="Status" value={inactive ? 'Inactive' : 'Active'} />
      </dl>
    </header>
  );
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="fact">
      <dt>{label}</dt>
      <dd className={mono ? 'mono' : undefined}>{value}</dd>
    </div>
  );
}

function Tag({ children, tone }: { children: React.ReactNode; tone?: 'ok' | 'warn' | 'fail' }) {
  return <span className={`tag${tone ? ` ${tone}` : ''}`}>{children}</span>;
}

/** No session handle in the URL means the app was opened directly rather than launched from the chart. */
function Launch() {
  return (
    <div className="launch">
      <h1>Patient Dashboard</h1>
      <p>
        This dashboard is launched from the OpenEMR chart. It authenticates through SMART on FHIR
        (OAuth2 / OpenID Connect) and receives a session handle from the agent.
      </p>
      <p className="reason">No session handle on this page — it was opened directly rather than through the SMART callback.</p>
      <a className="btn" href="/smart/launch">Launch from OpenEMR</a>
    </div>
  );
}
