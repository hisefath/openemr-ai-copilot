/* Clinical Co-Pilot panel: USERS.md between-rooms loop, ARCHITECTURE §1 (browser boundary) and §8 (API).
   The session handle lives only in this closure and travels as a bearer header: never storage, cookies or the URL.
   Every dynamic string goes through textContent, because chart text can carry injection attempts (FM-11). */
(function () {
    'use strict';

    const meta = document.querySelector('meta[name="copilot-session"]');
    const sessionHandle = meta ? meta.content : '';
    if (meta) meta.remove();
    // The SMART callback URL carries code and state; drop them from history (ARCHITECTURE §2).
    if (window.location.search) window.history.replaceState(null, '', window.location.pathname);

    const POLL_MS = 1500;
    const POLL_MAX_MS = 20000;
    const SESSION_TIMEOUT_MS = 5000;
    const QUESTION_TIMEOUT_MS = 15000;  // server deadline is 9 s (§4.2); this only ends a hung connection

    // §8 error statuses; anything else (404, 5xx, unparseable) is 'error'.
    const HTTP_FAILURE = {401: 'expired', 403: 'forbidden', 409: 'conflict', 429: 'busy'};
    const TEXT = {
        expired: 'Session expired: relaunch from the chart.',
        forbidden: 'Not permitted for your account.',
        conflict: 'Another question is still running. Wait for its answer.',
        busy: 'Too many requests. Wait a few seconds and try again.',
        timeout: 'The Co-Pilot did not respond in time. Try again, or check the chart directly.',
        error: 'The Co-Pilot could not answer right now. Try again, or check the chart directly.',
        stillLoading: 'Some records are still loading. Answers will list them as unavailable.',
        statusUnknown: 'Could not check which records have loaded. Each answer lists what was checked.',
        noBanner: 'Patient details unavailable: confirm the patient in the chart.',
        checking: 'Checking the chart…',
        clarify: 'Which record did you mean?',
    };
    // Windowed labels: 'none recorded' is only true inside the prefetch query window (§3, render.WINDOWS).
    const RESOURCES = {
        patient: 'Patient', allergies: 'Allergies', medications: 'Medications', conditions: 'Problems',
        labs: 'Labs (18 mo)', vitals: 'Vitals (12 mo)', encounters: 'Encounters (24 mo)',
    };
    const LOAD = {
        pending: 'loading', ok: 'loaded', empty: 'none recorded', forbidden: 'not permitted',
        expired: 'session expired', error: 'unavailable', timeout: 'timed out',
    };
    const SECTIONS = {
        visit_context: 'Visit context', safety: 'Safety', recent_results: 'Recent results',
        changes: 'Changes', background: 'Background',
    };
    // §5 verifies attribution only (cited ids belong to this patient), not completeness.
    const OUTCOMES = {
        pass: 'Sources verified', pass_with_removals: 'Sources verified, items removed',
        fail: 'Fallback: no AI selection', refused: 'Refused', clarify: 'Needs clarification',
        partial: 'Partial: time limit reached',
    };

    let expired = false;
    let busy = false;
    let pollRun = 0;
    let bannerFilled = false;
    const $ = (id) => document.getElementById(id);

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    // Type plus the id's last 8 characters: OpenEMR uuids are time-ordered, so their leading characters repeat.
    function sourceChips(ids) {
        const list = el('span', 'chips');
        for (const raw of ids || []) {
            const id = String(raw);
            const slash = id.indexOf('/');
            const tail = id.slice(slash + 1);
            const chip = el('span', 'chip', tail.length > 8 ? `${id.slice(0, slash + 1)}…${tail.slice(-8)}` : id);
            chip.title = id;
            list.append(chip);
        }
        return list;
    }

    // OpenEMR's HH:MM exactly as sent, never converted to the browser zone (AUDIT DQ-11; matches render._hhmm).
    function wallTime(value) {
        const match = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(String(value));
        return match ? match[1] : String(value);
    }

    // Null fields are left out: a failed Patient fetch also leaves them null, and only ok/empty are chart facts.
    function fillBanner(nameNode, metaNode, banner) {
        nameNode.textContent = banner ? banner.name : TEXT.noBanner;
        const parts = banner ? [
            banner.birth_date && `DOB ${banner.birth_date}`,
            banner.age !== null && banner.age !== undefined && `Age ${banner.age}`,
            banner.sex && `Sex ${banner.sex}`,
            banner.mrn && `MRN ${banner.mrn}`,
        ] : [];
        metaNode.replaceChildren(...parts.filter(Boolean).map((part) => el('span', null, part)));
    }

    function fillTopBanner(banner) {
        bannerFilled = true;
        fillBanner($('banner-name'), $('banner-meta'), banner);
    }

    function bannerNode(banner) {
        const node = el('div', 'banner');
        const name = el('div', 'banner-name');
        const details = el('div', 'banner-meta');
        fillBanner(name, details, banner);
        node.append(name, details);
        return node;
    }

    // fetched_at is UTC (fhir._now); say so instead of converting, so it matches the coverage lines.
    function asOfText(value) {
        if (!value) return '';
        return `Data as of ${wallTime(value)}${/(Z|[+-]00:00)$/.test(String(value)) ? ' UTC' : ''}`;
    }

    function flagList(flags) {
        const rank = (flag) => (flag.severity === 'high' ? 0 : 1);
        const list = el('ul');
        for (const flag of [...(flags || [])].sort((a, b) => rank(a) - rank(b))) {
            const high = flag.severity === 'high';
            const item = el('li', high ? 'flag flag-high' : 'flag');
            item.append(el('span', 'flag-sev', high ? 'High' : 'Medium'), el('span', null, flag.message),
                sourceChips(flag.source_ids));
            list.append(item);
        }
        return list;
    }

    async function api(method, path, body, timeoutMs) {
        let res;
        try {
            res = await fetch(path, {
                method,
                headers: {'Authorization': 'Bearer ' + sessionHandle, 'Content-Type': 'application/json'},
                body: body === undefined ? undefined : JSON.stringify(body),
                credentials: 'omit',
                cache: 'no-store',
                referrerPolicy: 'no-referrer',
                signal: AbortSignal.timeout(timeoutMs),
            });
        } catch (err) {
            return {failure: err && err.name === 'TimeoutError' ? 'timeout' : 'error', correlationId: null};
        }
        let data = null;
        try {
            data = await res.json();
        } catch (err) {
            data = null;
        }
        if (res.ok && data) return {data};
        return {
            failure: HTTP_FAILURE[res.status] || 'error',
            correlationId: (data && data.error && data.error.correlation_id) || res.headers.get('X-Correlation-ID'),
        };
    }

    function expire() {
        expired = true;
        $('fatal').textContent = TEXT.expired;
        $('fatal').hidden = false;
        $('ask-controls').disabled = true;
        $('busy').textContent = '';
    }

    function showFailure(result) {
        if (result.failure === 'expired') return expire();
        $('busy').replaceChildren(el('span', null, TEXT[result.failure]));
        if (result.correlationId) $('busy').append(el('span', 'cid', ` Ref ${result.correlationId}`));
    }

    function renderSession(status, settled) {
        if (status.patient_banner || settled) fillTopBanner(status.patient_banner);
        $('data-as-of').textContent = asOfText(status.data_as_of);
        $('load-statuses').replaceChildren(...Object.entries(status.load_statuses || {}).map(([key, value]) =>
            el('li', `chip load-${value}`, `${RESOURCES[key] || key}: ${LOAD[value] || value}`)));
        $('flags').replaceChildren(...flagList(status.flags).children);
        $('flags-section').hidden = !(status.flags || []).length;
    }

    // Polls until every load has settled. Launch prefetch reports nothing until it finishes, so an empty map is pending.
    // Timeouts, network errors, 429 and 5xx are retried until the cap; only 'expired' stops early. A newer call
    // (after each answer, to follow §3 Freshness refetches) replaces a running one.
    async function loadSession() {
        const run = ++pollRun;
        const started = Date.now();
        while (!expired && run === pollRun) {
            const result = await api('GET', '/api/session', undefined, SESSION_TIMEOUT_MS);
            if (run !== pollRun) return;
            if (result.failure === 'expired') return expire();
            if (result.data) {
                const statuses = Object.values(result.data.load_statuses || {});
                const settled = statuses.length > 0 && !statuses.includes('pending');
                renderSession(result.data, settled);
                if (statuses.includes('expired')) return expire();
                if (settled) {
                    $('load-note').hidden = true;
                    return;
                }
            }
            if (Date.now() - started + POLL_MS > POLL_MAX_MS) {
                if (!bannerFilled) fillTopBanner(null);
                $('load-note').textContent = result.failure ? TEXT.statusUnknown : TEXT.stillLoading;
                $('load-note').hidden = false;
                return;
            }
            await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        }
    }

    function lineItem(line) {
        const item = el('li', 'line', line.text);
        if (line.older_than_12_months) item.append(el('span', 'old', 'older than 12 months'));
        item.append(sourceChips(line.source_ids));
        return item;
    }

    function renderAnswer(question, answer) {
        const card = el('article', 'card');
        card.tabIndex = -1;
        card.setAttribute('aria-label', 'Answer');
        const head = el('div', 'card-head');
        head.append(el('span', 'question', question),
            el('span', `badge outcome-${answer.outcome}`, OUTCOMES[answer.outcome] || answer.outcome));
        card.append(bannerNode(answer.patient_banner), head, el('div', 'asof', asOfText(answer.data_as_of)));
        if (answer.notice) card.append(el('p', 'notice', answer.notice));
        if ((answer.flags || []).length) card.append(flagList(answer.flags));
        for (const section of answer.sections || []) {
            const lines = el('ul');
            lines.append(...(section.lines || []).map(lineItem));
            card.append(el('h3', null, SECTIONS[section.section] || section.section), lines);
        }
        if ((answer.clarify || []).length) {
            const box = el('div', 'clarify');
            box.append(el('p', null, TEXT.clarify));
            for (const line of answer.clarify) {
                const button = el('button');
                button.type = 'button';
                button.append(el('span', null, line.text), sourceChips(line.source_ids));
                button.addEventListener('click', () => ask(question, line.source_ids[0]));
                box.append(button);
            }
            card.append(box);
        }
        if ((answer.coverage || []).length) {
            const coverage = el('ul', 'coverage');
            coverage.append(...answer.coverage.map((c) => el('li', null, c.text)));
            card.append(coverage);
        }
        const withheld = answer.withheld_count || 0;
        card.append(el('p', 'withheld', `${withheld} ${withheld === 1 ? 'statement' : 'statements'} withheld`),
            el('p', 'cid', `Ref ${answer.correlation_id}`));
        $('answers').prepend(card);
        card.focus();
    }

    async function ask(question, selectedSourceId) {
        if (busy || expired || !question) return false;
        busy = true;
        $('ask-controls').disabled = true;
        $('busy').textContent = TEXT.checking;
        const body = {question};
        if (selectedSourceId) body.selected_source_id = selectedSourceId;
        if (window.crypto && window.crypto.randomUUID) body.client_request_id = window.crypto.randomUUID();
        const result = await api('POST', '/api/session/messages', body, QUESTION_TIMEOUT_MS);
        busy = false;
        $('ask-controls').disabled = expired;
        if (result.failure) {
            showFailure(result);
            if (!expired) $('question').focus();
            return false;
        }
        $('busy').textContent = '';
        renderAnswer(question, result.data);
        loadSession();
        return true;
    }

    $('ask').addEventListener('submit', async (event) => {
        event.preventDefault();
        const input = $('question');
        if (await ask(input.value.trim())) input.value = '';
    });
    for (const button of document.querySelectorAll('[data-question]')) {
        button.addEventListener('click', () => ask(button.dataset.question));
    }

    // The controls ship disabled so a page whose script failed can't submit the question natively.
    if (sessionHandle) {
        $('ask-controls').disabled = false;
        loadSession();
    } else expire();
})();
