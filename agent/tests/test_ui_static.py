"""Browser pages, checked without a browser. Each test names the failure mode it guards against: the CSP and the
textContent-only rule (ARCHITECTURE §1), the handle never leaving page memory, and the §8 API contract as committed in
schemas.py."""
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

import render
import smart
from config import Settings
from schemas import (Coverage, Flag, LoadStatus, MessageRequest, MessageResponse, Outcome, PatientBanner,
                     PatientContext, RenderedLine, RenderedSection, ScanCounts, ScanPatient, ScheduleScanResponse,
                     Section, SessionStatus)

STATIC = Path(__file__).parent.parent / "static"
FIXTURES = Path(__file__).parent / "fixtures"
PAGES = {"panel.html": "panel.js", "schedule.html": "schedule.js"}
JS = list(PAGES.values())
# ARCHITECTURE §8: the endpoints each page may call, with their methods.
API = {"panel.js": {("GET", "/api/session"), ("POST", "/api/session/messages")},
       "schedule.js": {("POST", "/api/schedule/scan")}}
# JS variable name -> the schemas.py model it holds, per file.
READS = {"panel.js": {"status": SessionStatus, "answer": MessageResponse, "section": RenderedSection,
                      "line": RenderedLine, "flag": Flag, "banner": PatientBanner, "c": Coverage},
         "schedule.js": {"scan": ScheduleScanResponse, "patient": ScanPatient, "c": ScanCounts, "flag": Flag,
                         "banner": PatientBanner}}


class Page(HTMLParser):
    def __init__(self, text: str):
        super().__init__()
        self.tags: list = []
        self.inline: list = []
        self._open = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._open = tag

    def handle_endtag(self, tag):
        self._open = None

    def handle_data(self, data):
        if self._open in ("script", "style") and data.strip():
            self.inline.append(self._open)


def read(name: str) -> str:
    return (STATIC / name).read_text()


def call_args(js: str, name: str) -> list:
    """Argument text of every `name(...)` call, by balanced parentheses."""
    out = []
    for m in re.finditer(rf"\b{name}\(", js):
        depth, i = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(js[i], 0)
            i += 1
        out.append(js[m.end():i - 1])
    return out


def function_body(js: str, name: str) -> str:
    """Text between the braces of `function name(...) {...}`, by balanced braces."""
    i = js.index("{", js.index(f"function {name}("))
    depth, j = 1, i + 1
    while depth:
        depth += {"{": 1, "}": -1}.get(js[j], 0)
        j += 1
    return js[i + 1:j - 1]


def object_items(js: str, const: str) -> dict:
    body = re.search(rf"const {const} = \{{(.*?)\}};", js, re.S).group(1)
    return dict(re.findall(r"(\w+):\s*'([^']*)'", body))


def object_keys(js: str, const: str) -> set:
    body = re.search(rf"const {const} = \{{(.*?)\}};", js, re.S).group(1)
    return set(re.findall(r"(\w+):\s*['\"]", body))


@pytest.mark.parametrize("page", PAGES)
def test_no_inline_script_style_or_event_handlers(page):
    """Guards: CSP script-src 'self' silently blocking the page, or an inline handler becoming an injection sink."""
    parsed = Page(read(page))
    assert parsed.inline == []
    for tag, attrs in parsed.tags:
        assert not [a for a in attrs if a.startswith("on")], tag
        assert "style" not in attrs, tag
        assert not any("javascript:" in (v or "").lower() for v in attrs.values()), tag
    assert [a["src"] for t, a in parsed.tags if t == "script"] == [f"/static/{PAGES[page]}"]


@pytest.mark.parametrize("page", PAGES)
def test_assets_are_same_origin_files_that_exist(page):
    """Guards: a CDN dependency the CSP blocks (and the hospital network may not reach), or a broken asset path."""
    for tag, attrs in Page(read(page)).tags:
        url = attrs.get("src") or (attrs.get("href") if tag == "link" else None)
        if url:
            assert url.startswith("/static/") and (STATIC / url.removeprefix("/static/")).is_file(), url


@pytest.mark.parametrize("page", PAGES)
def test_smart_panel_response_injects_one_handle_meta_before_the_script(page):
    """Guards: a page shipped with no handle (every call 401s) or two, because the page and smart.panel_response
    disagree on where the meta goes. Both callbacks (/smart/callback, /schedule/callback) serve pages this way (§2)."""
    text = read(page)
    assert "copilot-session" not in text and text.count("<head>") == 1
    settings = Settings.model_construct(openemr_public_origin="https://emr.example")
    body = smart.panel_response(settings, text, "h" * 43).body.decode()
    meta = '<meta name="copilot-session" content="' + "h" * 43 + '">'
    assert body.count("copilot-session") == 1
    assert body.index("<head>") < body.index(meta) < body.index("<script") < body.index("</head>")


@pytest.mark.parametrize("js", JS)
def test_no_html_sinks_eval_or_browser_storage(js):
    """Guards: chart text rendered as markup (FM-11), string-evaluated code, or the handle persisted outside memory."""
    text = read(js)
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                   "localStorage", "sessionStorage", "document.cookie", "indexedDB", "setTimeout('", 'setTimeout("',
                   "setInterval('", 'setInterval("', "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource",
                   "postMessage", "console."):
        assert banned not in text, banned


@pytest.mark.parametrize("js", JS)
def test_handle_read_from_meta_removed_and_used_only_as_bearer(js):
    """Guards: the session handle left in the DOM or copied into the URL, storage, logs or rendered text (§1)."""
    text = read(js)
    assert "document.querySelector('meta[name=\"copilot-session\"]')" in text
    assert "if (meta) meta.remove();" in text
    assert "history.replaceState(null, '', window.location.pathname)" in text
    allowed = [r"const sessionHandle = meta \? meta\.content : '';", r"'Authorization': 'Bearer ' \+ sessionHandle",
               r"if \(!?sessionHandle\)"]
    uses = [line.strip() for line in text.splitlines() if "sessionHandle" in line]
    assert len(uses) == 3
    assert all(any(re.search(p, use) for p in allowed) for use in uses), uses


@pytest.mark.parametrize("js", JS)
def test_every_fetch_is_same_origin_with_bearer_and_no_cookies(js):
    """Guards: an API call without the bearer handle (401 loop) or one that sends cookies or leaves the origin."""
    text = read(js)
    fetches = call_args(text, "fetch")
    assert fetches
    for args in fetches:
        assert args.startswith("path,")
        assert "'Authorization': 'Bearer ' + sessionHandle" in args
        assert "credentials: 'omit'" in args
        assert "signal: AbortSignal.timeout(timeoutMs)" in args
    assert not re.search(r"https?://|['\"`]//", text)


@pytest.mark.parametrize("js", JS)
def test_api_paths_and_methods_match_section_8(js):
    """Guards: the UI calling an endpoint or method the server does not define (ARCHITECTURE §8)."""
    text = read(js)
    calls = set(re.findall(r"\bapi\('(GET|POST)', '([^']+)'", text))
    assert calls == API[js]
    assert set(re.findall(r"['\"`](/api/[^'\"`]*)", text)) == {path for _, path in API[js]}


@pytest.mark.parametrize("js", JS)
def test_fields_read_exist_in_schemas(js):
    """Guards: the UI reading a response field that schemas.py does not define, so it renders blank or 'undefined'."""
    text = read(js)
    for var, model in READS[js].items():
        fields = set(re.findall(rf"\b{var}\.(\w+)", text))
        assert fields, var
        assert fields <= set(model.model_fields), (var, fields - set(model.model_fields))


def test_message_request_body_matches_schema():
    """Guards: the panel sending a field MessageRequest rejects, e.g. a misspelled selected_source_id."""
    text = read("panel.js")
    sent = {"question"} | set(re.findall(r"\bbody\.(\w+) =", text))
    assert "const body = {question};" in text
    assert sent == set(MessageRequest.model_fields)


def test_enum_labels_cover_every_schema_value():
    """Guards: a load status, outcome or section rendering as a raw code (or not at all) after a schema change."""
    panel, schedule = read("panel.js"), read("schedule.js")
    assert object_keys(panel, "LOAD") == {s.value for s in LoadStatus}
    assert object_keys(panel, "OUTCOMES") == {o.value for o in Outcome}
    assert object_keys(panel, "SECTIONS") == {s.value for s in Section}
    resources = set(PatientContext.model_fields) - {"patient_id", "fetched_at"}
    assert object_keys(panel, "RESOURCES") == object_keys(schedule, "RESOURCES") == resources


def test_load_chips_name_the_prefetch_window():
    """Guards: 'Labs: none recorded' read as 'no labs on file' when only the last 18 months were queried (§3)."""
    labels = object_items(read("panel.js"), "RESOURCES")
    for kind, window in render.WINDOWS.items():
        months = re.fullmatch(r"last (\d+) months", window).group(1) if window else None
        assert labels[kind].endswith(f"({months} mo)") if months else "(" not in labels[kind], kind


@pytest.mark.parametrize("js", JS)
def test_http_failures_map_to_fixed_texts(js):
    """Guards: a 401 not expiring the session, or a 403/409 told to 'try again' when retrying can't help (§8)."""
    text = read(js)
    mapping = object_items(text, "HTTP_FAILURE")
    assert mapping == {"401": "expired", "403": "forbidden", "409": "conflict", "429": "busy"}
    assert "failure: HTTP_FAILURE[res.status] || 'error'," in text
    assert "err.name === 'TimeoutError' ? 'timeout' : 'error'" in text
    assert set(mapping.values()) | {"timeout", "error"} <= object_keys(text, "TEXT")


@pytest.mark.parametrize("js", JS)
def test_times_are_shown_as_sent_not_converted_to_the_browser_zone(js):
    """Guards: appointment times shifted by the browser's UTC offset (AUDIT DQ-11: clinic time labelled +00:00), or
    a header 'as of' that disagrees with the server's coverage text."""
    text = read(js)
    assert "toLocale" not in text and "new Date(" not in text and "getHours" not in text
    pattern = re.search(r"const match = /(.+?)/\.exec\(String\(value\)\);", function_body(text, "wallTime")).group(1)
    starts = re.findall(r'"start": "([^"]+)"', (FIXTURES / "appointments_today.json").read_text())
    assert starts and [re.match(pattern, s).group(1) for s in starts] == [s[11:16] for s in starts]
    assert re.match(pattern, "2026-09-17T14:42:00+00:00").group(1) == render._hhmm("2026-09-17T14:42:00+00:00")


@pytest.mark.parametrize("js", JS)
def test_source_chips_show_the_distinguishing_end_of_the_id(js):
    """Guards: every chip reading 'MedicationRequest/a2c40f0a…' because OpenEMR uuids share their time-ordered prefix."""
    body = function_body(read(js), "sourceChips")
    assert "tail.slice(-8)" in body and "chip.title = id;" in body
    for name in ("patient_a.json", "patient_b.json"):
        ids = set(re.findall(r'"id": "([0-9a-f-]{36})"', (FIXTURES / name).read_text()))
        assert len(ids) > 100 and len({i[-8:] for i in ids}) == len(ids), name
        assert len({i[:8] for i in ids}) < len(ids) / 10, name  # the old prefix chip really collided


@pytest.mark.parametrize("js", JS)
def test_banner_leaves_out_missing_fields(js):
    """Guards: 'MRN not recorded' shown as a chart fact when the Patient fetch failed and every field is null."""
    text = read(js)
    assert "not recorded" not in function_body(text, "fillBanner" if js == "panel.js" else "bannerNode")
    for field, label in (("birth_date", "DOB"), ("sex", "Sex"), ("mrn", "MRN")):
        assert f"banner.{field} && `{label} ${{banner.{field}}}`" in text


def test_controls_are_inert_until_the_script_runs():
    """Guards: a native GET submit putting the question text in the URL and proxy logs when the script fails to load."""
    panel, schedule = read("panel.html"), read("schedule.html")
    assert '<fieldset id="ask-controls" disabled>' in panel and 'method="post"' in panel
    assert '<button class="primary" id="scan" type="button" disabled>' in schedule
    assert "$('ask-controls').disabled = false;" in read("panel.js")
    assert "if (sessionHandle) $('scan').disabled = false; else expire();" in read("schedule.js")


@pytest.mark.parametrize("page", PAGES)
def test_every_element_id_used_by_js_exists(page):
    """Guards: a renamed id making getElementById return null and the page throw on load."""
    ids = {a["id"] for _, a in Page(read(page)).tags if "id" in a}
    used = set(re.findall(r"\$\('([\w-]+)'\)", read(PAGES[page])))
    assert used and used <= ids, used - ids


def test_panel_quick_buttons_and_question_box():
    """Guards: the USERS.md one-tap questions missing, or a question longer than MessageRequest allows."""
    text = read("panel.html")
    labels = [html.unescape(b) for b in re.findall(r'<button type="button" data-question="[^"]+">([^<]+)</button>', text)]
    assert labels == ["Brief me", "Allergies & interactions", "What changed since last visit"]
    assert 'maxlength="2000"' in text and MessageRequest.model_json_schema()["properties"]["question"]["maxLength"] == 2000


def test_answer_cards_repeat_banner_and_show_verification_details():
    """Guards: an answer card without patient identity (§2) or hiding withheld counts, age markers or the support id."""
    panel, schedule = read("panel.js"), read("schedule.js")
    assert "bannerNode(answer.patient_banner)" in panel and "bannerNode(patient.patient_banner)" in schedule
    for needle in ("'older than 12 months'", "'statements'} withheld", "answer.correlation_id", "answer.coverage",
                   "answer.withheld_count", "answer.notice", "ask(question, line.source_ids[0])"):
        assert needle in panel, needle
    assert "Session expired: relaunch from the chart" in panel


def test_session_polling_is_bounded_and_survives_failures():
    """Guards: polling forever; stopping on the first empty status map (prefetch still running) or first network blip,
    so high-severity flags never appear; or top flags going stale after §3 Freshness refetches."""
    panel = read("panel.js")
    assert "const POLL_MS = 1500;" in panel and "const POLL_MAX_MS = 20000;" in panel
    body = function_body(panel, "loadSession")
    assert "if (Date.now() - started + POLL_MS > POLL_MAX_MS) {" in body
    assert "const settled = statuses.length > 0 && !statuses.includes('pending');" in body
    assert [line.strip() for line in body.splitlines() if "return" in line] == [
        "if (run !== pollRun) return;", "if (result.failure === 'expired') return expire();",
        "if (statuses.includes('expired')) return expire();", "return;", "return;"]
    assert "showFailure" not in body and "$('busy')" not in body
    assert "TEXT.statusUnknown : TEXT.stillLoading" in body
    assert "if (status.patient_banner || settled) fillTopBanner(status.patient_banner);" in function_body(panel, "renderSession")
    assert "renderAnswer(question, result.data);\n        loadSession();" in function_body(panel, "ask")


def test_outcome_badge_claims_attribution_not_completeness():
    """Guards: a green 'Verified' badge read as 'clinically complete' when §5 only checks cited ids belong to the patient."""
    outcomes = object_items(read("panel.js"), "OUTCOMES")
    assert outcomes["pass"] == "Sources verified" and outcomes["pass_with_removals"].startswith("Sources verified")


def test_schedule_waits_after_a_client_timeout():
    """Guards: a re-click after the 90 s client timeout starting a second 20-patient fan-out while the first still runs."""
    schedule = read("schedule.js")
    assert "const SCAN_COOLDOWN_MS = 30000;" in schedule
    assert "result.failure === 'timeout' ? SCAN_COOLDOWN_MS : 0" in function_body(schedule, "scan")
    assert "$('scan').disabled = false;" not in function_body(schedule, "scan")
