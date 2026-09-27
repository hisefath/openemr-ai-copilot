/**
 * One clinical card: its own fetch, its own loading, error and empty states.
 *
 * This component is the whole argument for the migration in miniature. The PHP dashboard rendered every
 * panel inside one server-side pass, so the slowest query set the latency of the entire page and a single
 * failing query could blank the screen. Here each card owns its request. A slow Care Team is a slow Care
 * Team; the other five are already on screen and interactive.
 */
import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { fetchCard, type CardKey, type CardResponse } from './api';
import type { AnyResource } from './fhir';

interface CardProps<T extends AnyResource> {
  cardKey: CardKey;
  title: string;
  /** Rendered once per resource. Returning null skips the row (e.g. a status we filter out). */
  row: (resource: T, index: number) => ReactNode;
  /** Shown when the chart genuinely records none of this — distinct from a failure. */
  emptyLabel?: string;
}

export function Card<T extends AnyResource>({ cardKey, title, row, emptyLabel }: CardProps<T>) {
  const q = useQuery({
    queryKey: ['card', cardKey],
    queryFn: () => fetchCard<T>(cardKey),
    // A clinician may sit on this screen for a whole appointment; refetching on window focus keeps a
    // second tab's edits from going stale without polling the API every few seconds.
    staleTime: 30_000,
  });

  return (
    <section className="card" aria-labelledby={`h-${cardKey}`} aria-busy={q.isPending}>
      <header className="card-head">
        <h2 id={`h-${cardKey}`}>{title}</h2>
        <CardBadge q={q} />
      </header>
      <div className="card-body">
        <CardContent q={q} row={row} emptyLabel={emptyLabel} title={title} />
      </div>
    </section>
  );
}

type Q<T extends AnyResource> = ReturnType<typeof useQuery<CardResponse<T>, Error>>;

function CardBadge<T extends AnyResource>({ q }: { q: Q<T> }) {
  if (q.isPending) return <span className="badge load">loading</span>;
  if (q.isError) return <span className="badge fail">unavailable</span>;
  const { status, resources } = q.data!;
  if (status === 'forbidden') return <span className="badge warn">not permitted</span>;
  if (status === 'expired') return <span className="badge warn">session expired</span>;
  if (status !== 'ok' && status !== 'empty') return <span className="badge fail">{status}</span>;
  return <span className="badge count">{resources.length}</span>;
}

function CardContent<T extends AnyResource>({
  q, row, emptyLabel, title,
}: { q: Q<T>; row: CardProps<T>['row']; emptyLabel?: string; title: string }) {
  if (q.isPending) {
    return (
      <ul className="rows" aria-hidden="true">
        {[0, 1, 2].map((i) => <li key={i} className="row skeleton" />)}
      </ul>
    );
  }

  if (q.isError) {
    return (
      <div className="state fail">
        <p>Could not load {title.toLowerCase()}.</p>
        <p className="reason">{q.error.message}</p>
        <button type="button" onClick={() => q.refetch()}>Retry</button>
      </div>
    );
  }

  const { status, resources } = q.data!;

  if (status === 'forbidden') {
    return <div className="state warn"><p>Your role does not permit viewing {title.toLowerCase()}.</p></div>;
  }
  if (status === 'expired') {
    return <div className="state warn"><p>Session expired. Relaunch from the chart.</p></div>;
  }
  if (status !== 'ok' && status !== 'empty') {
    return (
      <div className="state fail">
        <p>{title} unavailable ({status}).</p>
        <button type="button" onClick={() => q.refetch()}>Retry</button>
      </div>
    );
  }

  const rendered = resources.map(row).filter(Boolean);
  if (!rendered.length) {
    // "None recorded" is a statement about the chart. It is deliberately not the same UI as a failure.
    return <div className="state empty"><p>{emptyLabel ?? `No ${title.toLowerCase()} recorded.`}</p></div>;
  }

  return <ul className="rows">{rendered.map((node, i) => <li key={i} className="row">{node}</li>)}</ul>;
}
