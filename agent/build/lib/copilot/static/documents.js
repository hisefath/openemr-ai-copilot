/* Clinical Co-Pilot document panel: upload a scan, see what was read, and decide what reaches the chart.

   The session handle is NOT in this file. panel.js owns it and passes in an `api` function already bound to the
   bearer header, so the handle stays in one closure — same rule as Week 1.

   Every dynamic string goes through textContent. Document text is attacker-controlled in the plainest sense:
   whoever fills in the form chooses it, and it arrives here having been read off a page by a model. */
(function () {
    'use strict';

    const UPLOAD_TIMEOUT_MS = 120000;   // server ingest deadline is 90 s; this only ends a hung connection
    const DECISION_TIMEOUT_MS = 30000;

    const KIND_LABEL = {
        allergy: 'Allergy', medication: 'Medication', medical_problem: 'Problem', lab: 'Lab result',
    };
    // Lab values have nowhere to be written: OpenEMR 8.5 has no lab-result write route. Saying so in the UI is
    // better than an Approve button that silently does nothing.
    const NO_WRITE_ROUTE = 'lab';

    function mount(root, api, el) {
        let currentDoc = null;      // { document_id, pages: [{page,width,height}] }
        let boxes = [];             // { page, bbox, label } for the overlay
        const nodes = {};

        function build() {
            root.replaceChildren();
            const form = el('form', 'doc-upload');
            const fields = el('fieldset', 'doc-upload-row');
            const label = el('label', null, 'Attach a lab PDF or intake form');
            label.setAttribute('for', 'doc-file');

            const file = document.createElement('input');
            Object.assign(file, {type: 'file', id: 'doc-file', name: 'file', accept: 'application/pdf', required: true});

            const kind = document.createElement('select');
            kind.id = 'doc-type';
            [['intake_form', 'Intake form'], ['lab_pdf', 'Lab PDF']].forEach(function (pair) {
                const opt = document.createElement('option');
                opt.value = pair[0];
                opt.textContent = pair[1];
                kind.append(opt);
            });

            const submit = el('button', 'primary', 'Attach & read');
            submit.type = 'submit';

            fields.append(file, kind, submit);
            form.append(label, fields);
            form.addEventListener('submit', function (e) {
                e.preventDefault();
                upload(file.files[0], kind.value, fields);
            });

            nodes.status = el('p', 'status');
            nodes.status.setAttribute('role', 'status');
            nodes.status.setAttribute('aria-live', 'polite');
            nodes.viewer = el('div', 'doc-viewer');
            nodes.viewer.hidden = true;
            nodes.queue = el('div', 'doc-queue');

            root.append(form, nodes.status, nodes.viewer, nodes.queue);
        }

        async function upload(file, docType, fields) {
            if (!file) return;
            fields.disabled = true;
            nodes.status.textContent = 'Reading the document…';
            const body = new FormData();
            body.append('file', file);
            body.append('doc_type', docType);
            try {
                const res = await api('POST', '/api/session/documents', body, UPLOAD_TIMEOUT_MS);
                render(res);
            } catch (err) {
                nodes.status.textContent = (err && err.message) || 'The document could not be attached.';
            } finally {
                fields.disabled = false;
            }
        }

        function render(res) {
            currentDoc = {document_id: res.document.document_id, pages: res.pages || []};
            if (!res.extraction) {
                // The document IS stored and citable. Say that, rather than implying the upload failed.
                nodes.status.textContent =
                    'Stored in the chart, but it could not be read (' + (res.reason || 'unknown') + '). '
                    + 'Nothing has been added to the record.';
                nodes.viewer.hidden = true;
                nodes.queue.replaceChildren();
                return;
            }
            const parts = ['Stored in the chart.', res.located + ' of ' + res.total + ' values located on the page.'];
            if (res.located < res.total) parts.push('Unlocated values are shown but not boxed.');
            if (res.truncated) parts.push('Only the first pages were read.');
            parts.push('Nothing reaches the record until you approve it.');
            nodes.status.textContent = parts.join(' ');

            boxes = collectBoxes(res.extraction);
            showPage(1);
            loadQueue();
        }

        function collectBoxes(extraction) {
            const out = [];
            const push = function (citation, label) {
                if (citation && citation.bbox) out.push({bbox: citation.bbox, label: label});
            };
            (extraction.results || []).forEach(function (r) { push(r.citation, r.test_name + ' ' + r.value); });
            (extraction.allergies || []).forEach(function (a) { push(a.citation, a.substance); });
            (extraction.medications || []).forEach(function (m) { push(m.citation, m.name); });
            (extraction.family_history || []).forEach(function (f) { push(f.citation, f.condition); });
            if (extraction.chief_concern) push(extraction.chief_concern.citation, extraction.chief_concern.value);
            const demo = extraction.demographics || {};
            Object.keys(demo).forEach(function (k) { if (demo[k]) push(demo[k].citation, demo[k].value); });
            return out;
        }

        function showPage(page) {
            nodes.viewer.hidden = false;
            nodes.viewer.replaceChildren();
            const frame = el('div', 'doc-page');
            const img = document.createElement('img');
            img.alt = 'Page ' + page + ' of the attached document';
            img.src = '/api/session/documents/' + encodeURIComponent(currentDoc.document_id)
                + '/page/' + page + '.png';
            frame.append(img);

            // Boxes are placed as a PERCENTAGE of the page's own dimensions, so the overlay stays aligned at any
            // rendered size and on any paper size — never by assuming Letter or hard-coding the render scale.
            const size = (currentDoc.pages || []).filter(function (p) { return p.page === page; })[0]
                || {width: 612, height: 792};
            boxes.filter(function (b) { return b.bbox.page === page; }).forEach(function (b, i) {
                const box = el('span', 'doc-box');
                box.style.left = (100 * b.bbox.x0 / size.width) + '%';
                box.style.top = (100 * b.bbox.y0 / size.height) + '%';
                box.style.width = (100 * (b.bbox.x1 - b.bbox.x0) / size.width) + '%';
                box.style.height = (100 * (b.bbox.y1 - b.bbox.y0) / size.height) + '%';
                box.dataset.boxIndex = String(i);
                box.title = b.label;
                frame.append(box);
            });
            nodes.viewer.append(frame);

            if ((currentDoc.pages || []).length > 1) {
                const pager = el('div', 'doc-pager');
                currentDoc.pages.forEach(function (p) {
                    const b = el('button', p.page === page ? 'active' : null, 'Page ' + p.page);
                    b.type = 'button';
                    b.addEventListener('click', function () { showPage(p.page); });
                    pager.append(b);
                });
                nodes.viewer.append(pager);
            }
        }

        function highlight(label) {
            nodes.viewer.querySelectorAll('.doc-box').forEach(function (box) {
                box.classList.toggle('lit', box.title === label);
            });
        }

        async function loadQueue() {
            const res = await api('GET', '/api/session/documents/'
                + encodeURIComponent(currentDoc.document_id) + '/facts', null, DECISION_TIMEOUT_MS);
            nodes.queue.replaceChildren();
            if (!res.facts.length) {
                nodes.queue.append(el('p', 'status', 'Nothing left to review for this document.'));
                return;
            }
            const heading = el('h2', null, 'Review before it reaches the chart');
            const list = el('ul', 'doc-facts');
            res.facts.forEach(function (fact) { list.append(factRow(fact)); });
            nodes.queue.append(heading, list);
        }

        function factRow(fact) {
            const li = el('li', fact.located ? 'doc-fact' : 'doc-fact unlocated');
            const value = Object.keys(fact.payload)
                .filter(function (k) { return k !== 'comments'; })
                .map(function (k) { return fact.payload[k]; })
                .join(' · ');

            const head = el('div', 'doc-fact-head');
            head.append(el('span', 'doc-kind', KIND_LABEL[fact.fact_kind] || fact.fact_kind),
                        el('span', 'doc-value', value));
            head.addEventListener('mouseenter', function () { highlight(value.split(' · ')[0]); });
            head.addEventListener('mouseleave', function () { highlight(null); });

            const where = fact.located
                ? 'Page ' + fact.citation.page_or_section + ', boxed on the image above'
                : 'Extracted, could not be located on the page';
            li.append(head, el('p', 'doc-where', where));

            const actions = el('div', 'doc-actions');
            if (fact.fact_kind === NO_WRITE_ROUTE) {
                li.append(el('p', 'doc-where',
                    'Kept with the document. OpenEMR has no write route for lab results, so this cannot be '
                    + 'added to the structured record.'));
            } else {
                const approve = el('button', 'primary', 'Approve → chart');
                const reject = el('button', null, 'Reject');
                approve.type = reject.type = 'button';
                approve.addEventListener('click', function () { decide(fact, 'approve', actions); });
                reject.addEventListener('click', function () { decide(fact, 'reject', actions); });
                actions.append(approve, reject);
            }
            li.append(actions);
            return li;
        }

        async function decide(fact, decision, actions) {
            actions.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
            try {
                const res = await api('POST', '/api/session/documents/'
                    + encodeURIComponent(fact.document_id) + '/facts/decision',
                    {field_path: fact.field_path, decision: decision}, DECISION_TIMEOUT_MS);
                if (decision === 'approve' && !res.written) {
                    // Never report success for a record the chart did not receive.
                    actions.append(el('span', 'doc-failed',
                        'Not written (' + (res.reason || 'unknown') + '). Still pending.'));
                    actions.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
                    return;
                }
                await loadQueue();
            } catch (err) {
                actions.append(el('span', 'doc-failed', (err && err.message) || 'That did not go through.'));
                actions.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
            }
        }

        build();
        return {reload: loadQueue};
    }

    window.CopilotDocuments = {mount: mount};
}());
