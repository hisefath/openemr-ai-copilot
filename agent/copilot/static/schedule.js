/* Morning schedule scan (USERS.md UC5, ARCHITECTURE §2 schedule launch, §4.3, §8 POST /api/schedule/scan).
   Same browser boundary as the panel (§1): handle only in this closure, sent as a bearer header, text via textContent.
   ponytail: el/sourceChips/banner/flag/api helpers are copied from panel.js; move to a shared file if a third page appears. */
(function () {
    'use strict';

    const meta = document.querySelector('meta[name="copilot-session"]');
    const sessionHandle = meta ? meta.content : '';
    if (meta) meta.remove();
    // The OAuth callback URL carries code and state; drop them from history (ARCHITECTURE §2).
    if (window.location.search) window.history.replaceState(null, '', window.location.pathname);

    const SCAN_TIMEOUT_MS = 90000;  // SLO is 60 s for 20 patients (§4.3)
    // ponytail: §8 returns the scan as one POST response, so nothing renders progressively and a client timeout
    // can't stop the server fan-out. Wait before allowing another; per-session scan de-duplication on the server is the fix.
    const SCAN_COOLDOWN_MS = 30000;

    // §8 error statuses; anything else (404, 5xx, unparseable) is 'error'.
    const HTTP_FAILURE = {401: 'expired', 403: 'forbidden', 409: 'conflict', 429: 'busy'};
    const TEXT = {
        expired: 'Session expired: reopen the schedule scan and sign in again.',
        forbidden: 'Not permitted for your account.',
        conflict: 'A scan is already running. Wait for it to finish.',
        busy: 'Too many requests. Wait a few seconds and try again.',
        timeout: 'The scan did not finish in time and may still be running. Wait 30 s before trying again, or review the schedule directly.',
        error: 'The scan could not run right now. Try again, or review the schedule directly.',
        noBanner: 'Patient details unavailable: confirm the patient in the chart.',
        scanning: "Scanning today's schedule…",
        done: 'Scan complete.',
    };
    const RESOURCES = {
        patient: 'Patient', allergies: 'Allergies', medications: 'Medications', conditions: 'Problems',
        labs: 'Labs', vitals: 'Vitals', encounters: 'Encounters',
    };

    let expired = false;
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

    // Null fields are left out: a failed Patient fetch also leaves them null, and only ok/empty are chart facts.
    function bannerNode(banner) {
        const node = el('div', 'banner');
        const details = el('div', 'banner-meta');
        const parts = banner ? [
            banner.birth_date && `DOB ${banner.birth_date}`,
            banner.age !== null && banner.age !== undefined && `Age ${banner.age}`,
            banner.sex && `Sex ${banner.sex}`,
            banner.mrn && `MRN ${banner.mrn}`,
        ] : [];
        details.append(...parts.filter(Boolean).map((part) => el('span', null, part)));
        node.append(el('div', 'banner-name', banner ? banner.name : TEXT.noBanner), details);
        return node;
    }

    // OpenEMR sends clinic wall-clock time labelled +00:00 (AUDIT DQ-11): show HH:MM as sent, never convert.
    function wallTime(value) {
        const match = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(String(value));
        return match ? match[1] : String(value);
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
        $('scan').disabled = true;
    }

    const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

    function patientCard(patient) {
        const card = el('article', 'card');
        const failed = patient.status === 'failed';
        const head = el('div', 'card-head');
        head.append(
            el('span', null, patient.appointment_start ? `Appointment ${wallTime(patient.appointment_start)}` : 'Appointment time not recorded'),
            el('span', `badge outcome-${failed ? 'fail' : 'clarify'}`, failed ? 'Failed to load' : 'Flagged'));
        card.append(bannerNode(patient.patient_banner), head);
        if ((patient.failed_resources || []).length) {
            const names = patient.failed_resources.map((r) => RESOURCES[r] || r).join(', ');
            card.append(el('p', 'notice', `Not checked (failed to load): ${names}`));
        }
        if ((patient.flags || []).length) card.append(flagList(patient.flags));
        return card;
    }

    // ponytail: ScheduleScanResponse carries counts, not the §4.3 sentence, so this fixed template builds it from integers.
    function renderScan(scan) {
        const c = scan.counts;
        $('counts').textContent =
            `${plural(c.scheduled, 'appointment')} today · ${c.checked} checked · ${c.failed} failed to load · ${c.flagged} flagged`;
        $('notice').textContent = scan.notice;
        $('patients').replaceChildren(...(scan.patients || []).map(patientCard));
        $('cid').textContent = `Ref ${scan.correlation_id}`;
        $('result').hidden = false;
        $('counts').focus();
    }

    async function scan() {
        const started = Date.now();
        $('scan').disabled = true;
        $('result').hidden = true;
        $('progress').textContent = TEXT.scanning;
        const timer = setInterval(() => {
            $('elapsed').textContent = `${Math.round((Date.now() - started) / 1000)} s`;
        }, 1000);
        const result = await api('POST', '/api/schedule/scan', undefined, SCAN_TIMEOUT_MS);
        clearInterval(timer);
        $('elapsed').textContent = '';
        if (result.failure === 'expired') {
            $('progress').textContent = '';
            return expire();
        }
        setTimeout(() => { $('scan').disabled = expired; }, result.failure === 'timeout' ? SCAN_COOLDOWN_MS : 0);
        if (result.failure) {
            $('progress').replaceChildren(el('span', null, TEXT[result.failure]));
            if (result.correlationId) $('progress').append(el('span', 'cid', ` Ref ${result.correlationId}`));
            return;
        }
        $('progress').textContent = TEXT.done;
        renderScan(result.data);
    }

    $('scan').addEventListener('click', scan);
    // The button ships disabled so it stays inert if this script fails.
    if (sessionHandle) $('scan').disabled = false; else expire();
})();
