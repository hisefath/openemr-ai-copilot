# AUDIT.md — OpenEMR audit for the Clinical Co-Pilot

## Summary

OpenEMR is safe to *build on* only if the Co-Pilot treats the FHIR API as an unreliable, over-permissive witness. The audit found 71 verified issues (4 critical, 28 high). They reduce to five decisions that shaped the architecture.

**1. OpenEMR will not stop the agent from reading the wrong patient.** SMART `user/` scopes are checked against the user's *role*, never against a patient; the only per-patient hook is a stub that returns `true` (SEC-1, ARCH-3). `patient/` scopes do bind a patient but skip role ACL entirely, so front-desk staff could read clinical data (COMP-M1). Our prototype took `patient_id` from the browser. **Decision:** the agent takes the patient only from the SMART launch context on the server, and rejects any tool call for a different patient.

**2. "Empty" and "unknown" look exactly like "none".** FHIR cannot distinguish *no known allergies* from *nothing recorded* (DQ-1); free-text allergies come back as substance "Unknown" (DQ-2, ARCH-6); an unsupported search parameter returns HTTP 200 with an empty bundle instead of an error (ARCH-2); a physician's token gets 401 on some lab and note endpoints (SEC-2). Each would make an LLM say "no allergies" or "no recent labs" confidently. **Decision:** verification treats empty, unknown, and unauthorized as *unknown*, never as a negative fact, and the answer must say what it couldn't check.

**3. Medication data contradicts itself.** MedicationRequest merges two tables, so the same drug can appear twice with different statuses; prescriptions with any end date read "completed"; dosage maps strength as dose (DQ-3, DQ-4, DQ-M3). We observed this on import: 138 medications stored in both tables. **Decision:** tools deduplicate and normalize before the LLM sees data; conflicting records are shown with both sources, never silently resolved.

**4. PHI leaks into places that aren't the chart.** The prototype sent tool outputs (PHI) to Langfuse's non-HIPAA US cloud (COMP-3, critical); OpenEMR logs full PHI response bodies in plaintext by default (SEC-3, COMP-2); the agent had no audit trail of who asked about which patient, and its conversation cache could be replayed by another caller (COMP-6, COMP-7, SEC-5). And in our own deployment, **OpenEMR talked to MySQL without TLS** until we fixed it (OPS-2). **Decision:** traces carry ids, counts, timings and tokens only; the agent keeps its own audit log; sessions are bound to the authenticated user and patient; API logging is set to minimal; MySQL TLS is verified and required.

**5. Latency is dominated by OpenEMR, not the LLM.** Each FHIR call pays a full legacy bootstrap, ~10 auth queries, and a dozen synchronous audit writes (PERF-1, PERF-2, PERF-7); there is no server-side paging (PERF-4); MedicationRequest materializes a practice-wide UNION (PERF-3). On Railway's 1 GB container a fresh install took 480 s versus 32 s locally (OPS-3). An LLM-driven multi-round tool loop cannot meet a 5 s p50 (PERF-11). **Decision:** fetch a fixed, date-bounded set of resources in parallel at launch, cache per user+patient briefly, and make one LLM call per answer.

**What we would have missed by skipping the audit:** every one of these produces a *plausible, confident, wrong* answer rather than a crash. Happy-path demos with the admin account (which bypasses the physician ACL gaps) would have looked perfect.

## Method

- **Scope:** the fork at `Gauntlet-HQ/openemr-base-clean` (static review of `src/`, `library/`, `interface/`, `apis/`, `sql/`), the official `openemr/openemr:8.5.0` image we deploy (its startup scripts extracted from the registry and read line by line), our Railway deployment, and a local replica of it.
- **Data:** synthetic only. 24 Synthea patients (seed `20260917`) imported with OpenEMR's own CCDA importer into the local replica.
- **Process:** five auditors, one per required dimension, each required to cite file and line for every finding. Each dimension then got an independent verifier instructed to refute findings against the code: 1 finding refuted and dropped, 28 corrected (usually severity or line numbers), 15 missed issues added. Operational findings (OPS-*) come from deploying and operating the system, not from reading it.
- **Severity** is judged by impact on *this* product: a physician relying on a chart summary under time pressure.

## Operational findings (observed while deploying)

| ID | Severity | What happened | Evidence | Status |
|---|---|---|---|---|
| OPS-1 | critical | Attaching a persistent volume to `sites/` put OpenEMR into a crash loop: the empty volume looked like an old install needing an upgrade. Without the volume, a restart would re-run the installer over the live database (282 `DROP TABLE IF EXISTS`, ARCH-1). | Railway deploy logs: `Upgrade detected: 0 -> 14` / `Cannot upgrade - OpenEMR is not configured yet`, 11 restarts. `openemr.sh` step 3 and step 6. | **Fixed.** `sites/` restored onto the volume from a pre-change backup; start command waits for `sites/default/sqlconf.php` before starting OpenEMR, so a lost volume blocks instead of reinstalling. Two restart tests passed. |
| OPS-2 | high | OpenEMR's PHP connected to MySQL **without TLS**, and MySQL didn't require it. After configuring, the boot script's DB check failed because MySQL's auto-generated certificate has no hostname. | `src/BC/DatabaseConnectionFactory.php:40,101` only enable TLS if `sites/default/documents/certificates/mysql-ca` exists; `require_secure_transport=OFF`; `ERROR 2026: Hostname verification failed`. | **Fixed.** New CA and server cert for `mysql.railway.internal`, loaded with `SET PERSIST` + `ALTER INSTANCE RELOAD TLS`; CA installed as `mysql-ca`; `REQUIRE SSL` on the OpenEMR DB user; verified TLSv1.3 after restart. |
| OPS-3 | high | Railway's service memory limit is ~1 GB. Fresh install took 480 s (local: 32 s). Synthea data generation ran out of memory in the container. Raising the limit via config had no effect on this plan. | `/sys/fs/cgroup/memory.max = 999997440`; `java.lang.OutOfMemoryError`; deploy timing logs. | **Mitigated.** Data generated locally, imported with OpenEMR's importer. Memory ceiling is a load-test risk (not yet measured). |
| OPS-4 | medium | The `openemr/openemr:8.5.0` tag was republished during the week: a redeploy pulled a build with version marker 15 (was 14) and ran a database upgrade unannounced. | Deploy logs: `Upgrade database for default from 8.4.0`, `Completed: Upgrade to docker-version 15`. | **Fixed** (2026-09-18). The service now builds from [`deploy/openemr/Dockerfile`](deploy/openemr/Dockerfile), whose base is pinned to `openemr/openemr@sha256:65180ab2…` — the build everything was tested against. Upgrades are now a deliberate digest change. |
| OPS-5 | medium | CCDA import reported 24 successful imports but produced 23 patients: one record silently lost. **Root cause:** the importer builds a shell command without escaping (`contrib/util/ccda_import/import_ccda.php:194`, `exec("php … --document=" . $file)`), so a Synthea file named `…O'Keefe…xml` breaks the command, which the importer still counts as success. The same line is a **shell-injection** vector for crafted file names (admin-run CLI, local files only). | Importer output `Finished patients import 24` with `sh: syntax error: unterminated quoted string`; `SELECT COUNT(*) FROM patient_data` = 23. | **Open (upstream).** Fix is `escapeshellarg($file)`; until then, reconcile patient counts after every import and reject file names with shell metacharacters. |
| OPS-6 | low | Railway's volume contains `lost+found`, which OpenEMR's upgrader treats as a site. | Logs: `Start: Upgrade database for lost+found`, `site id 'lost+found' contains invalid characters`. | Harmless; documented. |
| OPS-7 | medium | The image's `OPENEMR_SETTING_*` env-to-globals sync uses `mariadb --skip-ssl … \|\| true`, which fails silently once TLS is required. | `devtoolsLibrary.source:160-173`; `openemr.sh:802,823`. | **Worked around.** Globals (`rest_fhir_api`, `site_addr_oath`, `api_log_option=1`) set over verified TLS. |
| OPS-8 | high | The first Claude request for a new structured-output schema and tool set took **20 s** (Anthropic compiles the grammar) versus **1.4 s** once warm, so the first physician question after every deploy would time out into the fallback. `/ready` also passed with **zero API credits**, because listing a model doesn't need credits. | Timed calls in the agent container: 19.99 s cold, 1.44 s warm; live `400 credit balance is too low` while `/ready` was green. | **Fixed:** warm-up request per request shape at startup (~$0.005 per deploy). Credits: documented as the first check in ALERTS.md A2. |
| OPS-9 | medium | OpenEMR on Railway is ~10× slower than the same image locally: CCDA import 17.6 s vs 1.8 s per patient; fresh install 480 s vs 32 s. | Importer timing output on both; Railway container `memory.max` ≈ 1 GB. | **Open.** Size the OpenEMR service up before load tests; this is the likeliest load-test bottleneck (LOAD_TEST.md). |

## Data defects observed in real OpenEMR FHIR output

Captured from OpenEMR 8.5.0's own FHIR services on imported Synthea patients ([agent/tests/fixtures](agent/tests/fixtures)). Each one produces a plausible, confident, wrong answer if passed straight to an LLM; each has a normalizer rule and a test ([agent/copilot/normalize.py](agent/copilot/normalize.py)).

| Observed | Consequence if unhandled | Handling |
|---|---|---|
| Every medication appears **twice**: an `intent=order` row with the drug, and an `intent=plan` row with only a name, the RxNorm code misfiled in `reasonCode` under the SNOMED system, and `authoredOn` = import date | Duplicate meds, half "started today" (DQ-M3) | Folded into one record with both source ids; status conflicts flagged |
| `dosageInstruction: [[]]` (invalid FHIR) | Crash, or invented dosage | Treated as "dosage not recorded" |
| A diphenhydramine order from **1968** still `status: active` | Stale drug stated as current (DQ-3) | "possibly stale" marker; rules say "may no longer be current" |
| 184 of 905 lab Observations have `valueString: "{entry.value}"` (template placeholder) | Placeholder cited as a result | Value treated as not recorded |
| 0 of 905 labs have reference ranges or abnormal flags | "No flag" read as "normal" (DQ-7) | Never says normal; critical ranges defined in rules.py |
| One patient returns **905 labs** in one unpaged call | Latency and token blow-up (PERF-4) | Date-bounded prefetch; per-test latest values in model context |
| Body height `163 [in_i]` (centimetres labelled inches) | "Height 13 ft" | `unit_suspect` flag |
| Every allergy (latex, peanut, bee venom, pollen) categorised `medication`; SNOMED codes labelled RxNorm | Wrong allergy class reasoning (DQ-2) | Classes derived from substance names, not category |
| Reaction `manifestation.text` holds severity words ("Mild", "Moderate") or `""` | "Reaction: Moderate" shown as a symptom | Moved to `severity`; empty means not recorded |
| Default OpenEMR appointment status maps to FHIR `proposed` | A schedule filter on `booked` would skip 4 of 8 real visits (UC5) | Scan excludes only cancelled, no-show and entered-in-error |
| A re-ordered drug merged with an older order took the older date | Current prescription labelled "may no longer be current" | Latest order wins date, status and staleness (regression test) |

## How the audit changed the plan

| Before the audit (prototype) | After the audit |
|---|---|
| `patient_id` sent by the browser | Patient taken from SMART launch context server-side; tool calls for other patients rejected (SEC-1, ARCH-3) |
| Session looked up by a client-supplied id | Session bound to authenticated user + patient + token, with a TTL (SEC-5, COMP-6) |
| LLM decides which FHIR calls to make, several rounds | Fixed, date-bounded parallel prefetch; one LLM call per answer (PERF-4, PERF-11) |
| Empty tool result = "none" | Empty, "Unknown", 401/403 and search errors = *unknown*, stated explicitly (DQ-1, DQ-2, ARCH-2, SEC-2) |
| Raw FHIR mapped straight to the prompt | Deduplicate meds across tables, drop data-absent vitals, surface conflicts with both sources (DQ-3, DQ-8, DQ-M3) |
| Langfuse captured tool outputs | Traces carry ids, counts, timings, tokens only; separate agent audit log (COMP-3, COMP-7) |
| Only citations verified | Citations **and** deterministic clinical rules (allergy ↔ drug class, dose thresholds, abnormal labs) |
| Tested with admin | Evals run as a non-admin physician user (SEC-2) |

## Appendix A — Full findings catalog

Every finding below was checked by a second, independent reviewer instructed to refute it against the code. One finding (DQ-6) was refuted and removed; corrections are shown inline. IDs ending in `-M#` were missed by the first auditor and added by the verifier.

### Security

| ID | Severity | Finding | Verification |
|---|---|---|---|
| [SEC-1](#sec-1) | high | SMART user/ scopes give access to every patient; no server-side binding to the open chart | Confirmed with corrections |
| [SEC-2](#sec-2) | high | Inconsistent FHIR gacl checks: patient/ launches skip role ACL; user/ reads of labs/notes require admin/super | Confirmed with corrections |
| [SEC-5](#sec-5) | high | Agent service: conversation sessions not bound to the caller; unauthenticated LLM invocation; PHI to Langfuse | Confirmed |
| [SEC-M1](#sec-m1) | high | FHIR API ignores the encounter 'sensitivities' and patient 'squads' ACLs that the UI enforces | Found by verifier |
| [SEC-3](#sec-3) | medium | Full FHIR response bodies (PHI) logged in plaintext to api_log and not attributed to the patient | Confirmed with corrections |
| [SEC-4](#sec-4) | medium | Confidential client secret not enforced at the token endpoint; tokens survive logout and deactivation | Confirmed with corrections |
| [SEC-7](#sec-7) | medium | Clinical PHI not encrypted at column level; CryptoGen keys silently regenerate if sites/ volume is lost | Confirmed |
| [SEC-8](#sec-8) | medium | Railway deployment: header-derived OAuth issuer, plaintext DB link, non-Secure core cookie, root DB creds in env | Confirmed |
| [SEC-9](#sec-9) | medium | MFA optional and unenforceable globally; weak lockout; OAuth login echoes password into HTML | Confirmed with corrections |
| [SEC-M2](#sec-m2) | medium | Indirect prompt injection from free-text chart fields; the answer text is never verified | Found by verifier |
| [SEC-6](#sec-6) | low | API error responses leak internal exception messages including full SQL statements | Confirmed |
| [SEC-10](#sec-10) | low | OIDC end-session endpoint: unsigned id_token_hint and open redirect | Confirmed |
| [SEC-M3](#sec-m3) | low | SMART v2 granular scope constraints are enforced only for Condition, Observation and RelatedPerson | Found by verifier |

#### SEC-1

**SMART user/ scopes give access to every patient; no server-side binding to the open chart** — *high*

- **Evidence:** - src/RestControllers/Authorization/BearerTokenAuthorizationStrategy.php:479-485: the stub returns true. Its only caller is :443 inside populateTokenContextForRequest (:434-455), where it gates only whether the launch patient is bound. It is never consulted for user/ reads.
  - apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php:75-80: isPatientRequest branch, else patients/med gacl.
  - src/RestControllers/Config/RestConfig.php:180-188: aclCheckCore, role-level only.
  - src/Common/Http/HttpRestRequest.php:421-425: "user scope overwrites user and patient".
  - src/Common/Http/HttpRestRouteHandler.php:64-66: a patient context marks the request as a patient request.
  - src/RestControllers/Subscriber/AuthorizationListener.php:143-150.
  - agent/main.py:106-109: ChatRequest.patient_id comes from the request body.
  - agent/main.py:144-147: session is checked only against the body patient_id.
  - agent/main.py:127-128: tool input is rejected if patient_id != req.patient_id.
- **Impact on the Co-Pilot:** Any user/ token the agent holds can read every patient's chart. The only thing keeping tools on the right patient is the agent's own check against a client-supplied patient_id. A UI bug, a tampered request, or a prompt injection that changes patient_id would pull another patient's PHI into the LLM context and the answer, and OpenEMR's audit log would not flag it (see SEC-3). This breaks the minimum-necessary principle and the safety premise of a chart-scoped copilot.
- **Recommendation:** - Use EHR launch with `launch` plus ONLY patient/*.rs scopes (never mix user/ and patient/ for the same resource). OpenEMR then enforces the patient binding through the token context (HttpRestRouteHandler.php:65-67, AuthorizationListener.php:143-150).
  - In the agent, derive patient_id from the token response's `patient` field and store it server-side keyed to the token. Reject any request whose patient_id differs.
  - If user/ scopes stay, implement checkUserHasAccessToPatient (or an agent-side allowlist such as today's schedule or care team) and document that OpenEMR itself has no per-patient ACL.
- **Verifier correction:** - Overstated as critical. SMART user/ scopes mean everything the user can already see, and OpenEMR core has no per-patient ACL for normal users (only squads and sensitivities). The token therefore gives no more than the clinician's existing UI access. The real gap is the agent binding to a patient_id supplied by the client, which is a wrong-patient and minimum-necessary risk: high, not critical.
  - The claim that a prompt injection could change patient_id is wrong. main.py:127 rejects any tool call whose patient_id differs from req.patient_id, so only the authenticated browser's request body can switch patients.
  - The recommendation to implement checkUserHasAccessToPatient would not restrict user/ reads, because it is only called when binding the launch patient (:443). Restricting user/ reads needs route or service changes, or an agent-side allowlist.

#### SEC-2

**Inconsistent FHIR gacl checks: patient/ launches skip role ACL; user/ reads of labs/notes require admin/super** — *high*

- **Evidence:** - Route ACL lines verified in apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php:
    - DiagnosticReport 228, 241 (admin/super)
    - DocumentReference 253, 279 (admin/super)
    - Binary 291 (admin/users)
    - Encounter search 303 (encounters/auth_a)
    - Encounter/:uuid 314 (admin/super)
    - Goal 326, 337 (admin/super)
    - Practitioner 658 (admin/users)
    - Provenance 762, 777 (admin/super)
    - Condition 167-175 and Observation 493-500 use FhirGenericRestController with patients/med
    - Patient 590 and 618 (patients/demo); AllergyIntolerance 79 and MedicationRequest 476, 487 (patients/med)
  - src/RestControllers/FHIR/FhirGenericRestController.php:93-101: same skip pattern confirmed.
  - The Physicians ACL is at library/classes/Installer.class.php:1152-1213. The 'write' block (1196-1203) grants patients appt/demo/med/.../lab, encounters auth_a..., sensitivities normal+high, and admin only 'drugs'.
  - src/FHIR/SMART/SmartLaunchController.php:114-160: redirectAndLaunchSmartApp does only a CSRF check, no gacl, before issuing a launch for the session pid.
- **Impact on the Co-Pilot:** - A real physician's user/ token gets 401 on lab reports, clinical notes, single encounters, goals and practitioner lookups. If the agent treats 401 as empty, it will tell the doctor "no recent labs or notes on file", a dangerous false negative.
  - Testing with the auto-configured admin account hides all of this.
  - The obvious workaround (making physicians admins) is massive over-privilege.
  - With patient/ scopes, any user who can launch the app (for example front office staff without patients/med) can read full clinical data for the launched patient, because gacl is never consulted.
- **Recommendation:** - Run the agent's evaluations with a non-admin user in the Physicians group, for both user/ and patient/ scope sets.
  - Treat HTTP 401/403 from FHIR as a tool error that is shown to the clinician ("not authorized to view labs"), never as an empty result.
  - If using patient/ scopes, add a gacl check for the launching user (for example, require patients/med via AclMain at launch, or in checkUserHasAccessToPatient). Also restrict which users see the launch button.
  - Do not grant admin/super to clinicians. If lab or note access is needed, patch those routes to patients/lab and patients/docs.
- **Verifier correction:** - Wrong line reference: Installer.class.php:1216-1233 is the Clinicians group, not Physicians. Physicians are at 1152-1213; the conclusion (no admin/super or admin/users) still holds.
  - The denial is HTTP 403 (AccessDeniedHttpException thrown at RestConfig.php:188), not 401.
  - Lab values exposed as Observation use patients/med, which physicians have. Only the DiagnosticReport lab report requires admin/super, so "401 on lab reports" applies to DiagnosticReport only.
  - The current agent tools (Patient, AllergyIntolerance, MedicationRequest) all pass for a Physicians-group user. The false-negative risk applies to planned tools.
  - The agent already returns FHIR failures to the model as is_error tool results (main.py:134-136), not as empty data.
  - The patient/ scope gacl bypass is confirmed and is the most serious part. It becomes reachable if SEC-1's recommendation (patient/ scopes only) is adopted.

#### SEC-5

**Agent service: conversation sessions not bound to the caller; unauthenticated LLM invocation; PHI to Langfuse** — *high*

- **Evidence:** - agent/main.py:
    - :121 SESSIONS dict with no TTL or owner
    - :144-147 session looked up by the client session_id, checked only against patient_id
    - :150 session_id and patient_id sent to Langfuse trace metadata
    - :142-143 prefix-only bearer check
    - :158 Claude called before any token validation
    - :169 tool results appended to the shared session history
    - :176 verify uses session['fetched']
    - :140 @observe(capture_input=False) on chat, so the ChatResponse with the PHI answer is captured as output
  - agent/fhir.py:57, 65, 79: capture_input=False only, so tool output (names, DOB, allergies, meds) is captured.
  - agent/requirements.txt: langfuse==3.7.0.
  - agent/.env.example:7: LANGFUSE_HOST=https://us.cloud.langfuse.com, the standard US cloud rather than a HIPAA or self-hosted endpoint.
- **Impact on the Co-Pilot:** - A second user (or anyone who can see Langfuse session ids) can replay a session_id and receive answers grounded in PHI fetched with another clinician's token. This defeats OpenEMR's authorization entirely.
  - Anyone who can reach /chat can run up Anthropic spend and probe prompt injection without a valid EHR token.
  - Sending PHI to Langfuse requires a BAA or self-hosting.
- **Recommendation:** - Validate the token before calling the LLM, for example with OpenEMR token introspection (/oauth2/default/introspect) or a cheap FHIR call. Cache the result briefly.
  - Bind each session to (token subject/fhirUser, client_id, patient) and reject mismatches. Generate session ids server-side.
  - Configure Langfuse output capture off or masked for tools, or use self-hosted or HIPAA-eligible Langfuse under a BAA.
  - Rate-limit /chat per user.
- **Verifier correction:** - Minor: session_id is a server-generated uuid4 when omitted (main.py:144), so replay needs a leaked id (from a response, Langfuse or logs); it cannot be guessed.
  - Raw LLM prompts are not sent to Langfuse, because the generation output is only stop_reason (main.py:162-163). Tool outputs and the final answer are sent.
  - The default output capture remains INFERENCE based on Langfuse v3 decorator defaults.

#### SEC-M1

**FHIR API ignores the encounter 'sensitivities' and patient 'squads' ACLs that the UI enforces** — *high*

- **Evidence:** - The UI hides restricted encounters and patients:
    - interface/patient_file/history/encounters.php:507 and interface/patient_file/encounter/forms.php:563: AclMain::aclCheckCore('sensitivities', $sensitivity)
    - interface/patient_file/summary/demographics.php:1069: aclCheckCore('squads', $result['squad'])
  - The FHIR read path never checks either:
    - src/Services/EncounterService.php:152-240: search() selects fe.sensitivity but never filters on it
    - EncounterService.php:449-450: the only sensitivity check is in the update path
    - `grep -rn sensitiv src/Services/FHIR` returns nothing
  - Clinicians are granted only 'sensitivities' => ['normal'] (library/classes/Installer.class.php:1235).
  - With patient/ scopes no gacl runs at all (SEC-2), and SmartLaunchController.php:114-160 does no ACL check at launch.
- **Impact on the Co-Pilot:** Encounters or notes marked 'high' sensitivity (typically behavioral health or substance-use care) are hidden from some users in the EHR UI but returned by FHIR. When encounter or note tools are added, the copilot would summarize these records to any user who launches it (with patient/ scopes) or who holds encounters/auth_a (with user/ scopes). It would also send them to Claude and Langfuse, silently bypassing an explicit confidentiality control. This gets worse if SEC-1's advice to use patient/ scopes only is followed.
- **Recommendation:** - Before adding Encounter, DocumentReference or clinical-note tools, filter out records whose form_encounter.sensitivity the launching user lacks (for example, a server-side check via AclMain, or a patch to EncounterService::search and the FHIR note services).
  - Until then, exclude sensitive encounter and note data from agent tools.
  - Include a 'high' sensitivity encounter in evaluations run as a Clinicians-group user.

#### SEC-3

**Full FHIR response bodies (PHI) logged in plaintext to api_log and not attributed to the patient** — *medium*

- **Evidence:** - library/globals.inc.php:2893-2902: api_log_option default '2'.
  - src/RestControllers/Subscriber/ApiResponseLoggerListener.php:
    - :64 `$logResponse = $response->getContent();`
    - :84-85 request_body and response are both set to the response body
    - :75 patientId comes from session 'pid'
  - src/RestControllers/Authorization/BearerTokenAuthorizationStrategy.php:267: pid is set only for the patient role.
  - sql/database.sql:92-105: api_log longtext columns.
  - src/Common/Logging/EventAuditLogger.php:660-661: encryption path removed.
- **Impact on the Co-Pilot:** - Each copilot question fans out to several FHIR reads, and each full JSON bundle (demographics, meds, allergies, labs) is copied into the audit tables in plaintext. That enlarges the breach blast radius and grows the tables quickly (performance and retention cost).
  - Because user-role reads are logged with patient_id 0, OpenEMR cannot produce a per-patient access report for agent-driven reads (HIPAA audit controls and accounting of disclosures).
- **Recommendation:** - Set api_log_option=1 (minimal) in production.
  - Keep a compact agent-side audit record per tool call: user id, patient id, resource type, record ids, correlation id and timestamp. Store it in an access-controlled store with a defined retention period.
  - Restrict SELECT on log, api_log and log_comment_encrypt to the app user.
  - If full logging is required, purge or encrypt api_log on a schedule.
- **Verifier correction:** - Line numbers are off by one: 63 should be 64, 85-86 should be 84-85, 74 should be 75.
  - The breach impact is overstated. api_log sits in the same MySQL database as clinical tables that are already plaintext (SEC-7). The added exposure is duplication into logs and backups, not a new trust boundary.
  - "Cannot produce a per-patient access report" is overstated. request_url records Patient/<uuid> or ?patient=<uuid>, so access can be rebuilt by parsing URLs. It cannot be done through the patient_id column or the built-in patient audit filter.
  - Table growth and retention cost remain valid concerns. Medium rather than high.

#### SEC-4

**Confidential client secret not enforced at the token endpoint; tokens survive logout and deactivation** — *medium*

- **Evidence:** - src/Common/Auth/OpenIDConnect/Repositories/ClientRepository.php:185-221:
    - :198 `if (!empty($clientSecret) && !empty($client['is_confidential']))`
    - :217 `return true;`
    - :219-220 "// password and refresh grant" returns true
  - src/Common/Auth/OpenIDConnect/Grant/CustomAuthCodeGrant.php:227-228 and CustomRefreshTokenGrant.php:181-183 delegate to parent::validateClient, which reaches this repository.
  - composer.lock:5116: league/oauth2-server 8.5.5.
  - AuthorizationController.php:110-111: PT1H access TTL, P3M refresh TTL.
  - src/Common/Auth/UuidUserAccount.php:51: user lookup has no active filter.
  - Trusted-user deletes exist only at AuthorizationController.php:1487 and src/FHIR/SMART/ClientAdminController.php:675.
  - IdTokenSMARTResponse.php:81-99 and RefreshTokenRepository.php:78-89: refresh tokens are issued only when offline_access is granted.
  - CustomAuthCodeGrant.php:253-270: S256 is enforced only when a code_challenge is sent; PKCE is not mandatory for confidential clients.
- **Impact on the Co-Pilot:** - The agent backend holds physician tokens. A leaked refresh token (from logs, Langfuse or a crash dump) or an intercepted auth code can be redeemed without the client secret.
  - Offboarding a clinician in OpenEMR does not cut off agent access that is already in flight (at least 1 hour for access tokens, up to 3 months with offline_access).
- **Recommendation:** - Always use PKCE S256, even as a confidential client.
  - Do not request offline_access; re-launch per session instead.
  - Keep tokens server-side only, never log them (the capture_input=False pattern is good).
  - Patch ClientRepository::validateClient to require and verify the secret or JWT whenever is_confidential=1, for every grant type.
  - Add `users.active=1` to the bearer user lookup.
  - Add an offboarding runbook step: revoke the user's tokens in Admin > System > API Clients.
- **Verifier correction:** - The code defect is real, but exploitation needs a leaked authorization code or a leaked refresh token first.
  - Refresh tokens, and with them the 3-month post-deactivation window, exist only if offline_access is requested. The auditor already advises against requesting it.
  - For this project, using PKCE and no offline_access reduces the exposure to at most one hour per access token. Medium rather than high.

#### SEC-7

**Clinical PHI not encrypted at column level; CryptoGen keys silently regenerate if sites/ volume is lost** — *medium*

- **Evidence:** - src/Common/Crypto/CryptoGen.php:6-11: split-key design.
  - CryptoGen.php:450-452: missing drive key triggers createDriveKey.
  - CryptoGen.php:461: throws only when a key file exists but cannot be decrypted.
  - Encryption call sites limited to ClientRepository, MfaUtils, SMARTLaunchToken, OAuth2KeyConfig, AuthGlobal, PaymentProcessing and module credentials (grep encryptStandard|encryptForDatabase).
  - library/globals.inc.php:1028-1033: "certain data".
  - src/Common/Auth/OAuth2KeyConfig.php:63-75: keys recreated if verifyKeys fails.
- **Impact on the Co-Pilot:** - Confidentiality of PHI at rest depends entirely on Railway volume and database protections.
  - If the sites/ volume is missing or remounted, OpenEMR boots normally but with new keys:
    - OAuth client secrets fail to decrypt (validateClient returns false, so the agent's client breaks)
    - clinicians' MFA TOTP secrets become unusable
    - existing tokens stop verifying
    - encrypted documents cannot be read
    The agent sees this as a sudden, opaque authentication outage.
- **Recommendation:** - Mount a persistent Railway volume at sites/ before first boot and verify that the methods/ and certificates/ files survive a redeploy.
  - Back up the drive keys separately from DB backups; both are needed, and storing them together defeats the split.
  - Confirm and document Railway disk encryption for both the MySQL and OpenEMR volumes (INFERENCE: not verifiable from code).
  - Add a boot-time alert when CryptoGen or OAuth2KeyConfig creates a new key on a site that is already configured.
- **Verifier correction:** - Accurate. INFERENCE: in the Docker image, losing sites/ also loses sqlconf.php, so the container may attempt re-setup rather than boot normally. The silent key-regeneration logic itself is confirmed.

#### SEC-8

**Railway deployment: header-derived OAuth issuer, plaintext DB link, non-Secure core cookie, root DB creds in env** — *medium*

- **Evidence:** - library/globals.inc.php:3243-3248: GlobalConnectorsEnum::SITE_ADDRESS_OAUTH ('site_addr_oath') default ''.
  - interface/globals.php:204-227: scheme from REQUEST_SCHEME; host from X-Forwarded-Host (last element, :208-211), then HTTP_HOST.
  - interface/globals.php:646-647.
  - src/FHIR/Config/ServerConfig.php:44.
  - src/BC/DatabaseConnectionOptions.php:140-147 and src/BC/DatabaseConnectionFactory.php:39-44: TLS only if mysql-ca exists.
  - src/Common/Session/SessionConfigurationBuilder.php:26 (cookie_secure false) and :88 (core cookie HttpOnly false). The OAuth cookie is Secure (:100).
  - docker/release/openemr.sh:54, 68; cleanup at 640-656.
  - library/sanitize.inc.php:35-39.
  - src/Common/Auth/AuthUtils.php:1206: IP lockout keyed on the full ip_string.
  - No X-Forwarded-Proto handling in docker/release/openemr.conf.
- **Impact on the Co-Pilot:** - Behind Railway's TLS edge, PHP sees http, so the issuer and expected audience can become http://host or follow attacker-supplied X-Forwarded-Host. SMART launch aud validation (CustomAuthCodeGrant.php:99-103) and token audience checks then fail or behave unpredictably.
  - Without mysql-ca, all PHI crosses the private network in the plaintext MySQL protocol. Supplying the auto-generated self-signed CA likely fails hostname verification (INFERENCE).
  - Root DB credentials stay available to any code-execution bug for the container's whole life.
  - IP lockout and audit IPs can be spoofed.
- **Recommendation:** - Set site_addr_oath to https://<railway-domain> explicitly.
  - Set session.cookie_secure=1 in php.ini.
  - Extract MySQL's ca.pem into documents/certificates/mysql-ca if verification can be made to work. Otherwise document reliance on Railway private-network encryption as an accepted risk.
  - Remove MYSQL_ROOT_USER and MYSQL_ROOT_PASS from service variables after first boot, and rotate OE_PASS.
  - Only trust X-Forwarded-For from the Railway proxy.
  - In the running container, verify that setup.php, admin.php, sql_patch.php and acl_upgrade.php are gone. They are $ignoreAuth=true (sql_patch.php:24, acl_upgrade.php:59); the entrypoint normally deletes them (openemr.sh:640-656).
- **Verifier correction:** - X-Forwarded-Host spoofing works only if Railway's edge does not set or append the header, because the code takes the last comma element (INFERENCE).
  - The IP issue is stronger than stated: behind the proxy, REMOTE_ADDR is the proxy, and lockout is keyed on "REMOTE_ADDR (XFF)". Rotating X-Forwarded-For therefore evades IP lockout.
  - docker/release/openemr.sh is the in-tree release script. The entrypoint of the official openemr/openemr:8.5.0 image may differ (INFERENCE).
  - The practical effect of the http issuer is more likely to break SMART launch aud checks (CustomAuthCodeGrant.php:101-104) than to be exploitable.

#### SEC-9

**MFA optional and unenforceable globally; weak lockout; OAuth login echoes password into HTML** — *medium*

- **Evidence:** - src/Common/Auth/MfaUtils.php:75-78.
  - No MFA enforcement global in library/globals.inc.php.
  - library/globals.inc.php:2113-2118 (timeout 7200), 2202-2207 (password_max_failed_logins 20), 2209-2214 (reset after 3600s), 2216-2221 (ip_max_failed_logins 100).
  - templates/oauth2/oauth2-login.html.twig:83: hidden password field on the MFA step, populated at src/RestControllers/AuthorizationController.php:827-831.
  - Skip flow: AuthorizationController.php:593-596 and 1653-1670; library/globals.inc.php:3299-3304; src/Common/Auth/OpenIDConnect/Entities/ClientEntity.php:75.
- **Impact on the Co-Pilot:** A copilot that assembles a patient summary in one click raises the value of a compromised clinician password. With the skip-login launch flow, the agent's token carries exactly the core login's assurance: password only, 2-hour idle sessions and 20 guesses per account. On the MFA step the password is also held in the page DOM and caches.
- **Recommendation:** - Adopt a policy that every account allowed to launch the copilot must enroll TOTP. Enforce it by only showing or enabling the launch for users with an MFA row, or patch the login to require MFA for those groups.
  - Lower the idle timeout (for example 900 to 1800s for shared exam-room workstations).
  - Tighten lockout to around 5 to 10 attempts.
  - Fix the template so it carries a server-side login-state token instead of the password.
- **Verifier correction:** - "EHR-launch skip flow is enabled by default" is overstated. The global default '1' only allows an admin to enable skipping for each client. ClientEntity's skipEHRLaunchAuthorizationFlow defaults to false, and shouldSkipAuthorizationFlow requires both the global and the client flag.
  - Line references corrected from 590-595 to 593-596.
  - The lockout counter resets hourly, so the real limit is about 20 guesses per account per hour, sustained.

#### SEC-M2

**Indirect prompt injection from free-text chart fields; the answer text is never verified** — *medium*

- **Evidence:** - Free-text database fields are copied verbatim into FHIR text:
    - src/Services/FHIR/FhirMedicationRequestService.php:463-465: prescriptions.drug becomes medicationCodeableConcept.text
    - FhirMedicationRequestService.php:264-266: dosage instructions become dosage.text
    - src/Services/FHIR/FhirAllergyIntoleranceService.php:209 and :221: lists.title is the fallback display and the narrative
  - agent/fhir.py:71, 85, 87 pass these strings through unchanged.
  - agent/main.py:133 json.dumps(records) goes straight into the tool_result.
  - The SYSTEM prompt (main.py:25-31) does not mark tool data as untrusted.
  - agent/verify.py:26-40 checks only briefing.claims, and main.py:180 returns briefing.answer unverified.
  - verify.py:34 is a case-insensitive substring match against json.dumps(record), so a quoted_value such as "source_id" or "active" matches any record, and a negated claim ("not allergic to penicillin" quoting "penicillin") passes.
- **Impact on the Co-Pilot:** Text typed into a medication name, dosage instruction or allergy title (by staff, CCDA import or migrated data) can steer the model's answer. The damage is limited because the tools are read-only and patient-locked, but a manipulated or hallucinated statement in `answer` can still be shown with verification_passed=true. For a physician with 90 seconds, this undermines the citation guarantee the design relies on.
- **Recommendation:** - Wrap tool results in explicit untrusted-data delimiters and tell the model to ignore instructions inside them.
  - Render only verified claims, or verify that every sentence in `answer` maps to a verified claim.
  - Tighten verify() to match quoted_value against specific field values of the cited record, not the whole JSON blob, and reject quoted values that are key names or trivially short.
  - Add injection strings in medication and allergy text to the evaluation set.

#### SEC-6

**API error responses leak internal exception messages including full SQL statements** — *low*

- **Evidence:** - src/RestControllers/Subscriber/ExceptionHandlerListener.php:50-55: 'message' => $exception->getMessage() is returned for all exceptions.
  - src/Common/Database/QueryUtils.php:85-91: SqlQueryException message includes the statement.
  - apis/dispatch.php:41-44.
  - src/RestControllers/AuthorizationController.php:624-629 and 1408-1416: raw getMessage() written into 500 bodies.
  - src/RestControllers/RestControllerHelper.php:324-326: FHIR internalErrors returned in the body.
  - Agent side: fhir.py:49 raise_for_status and main.py:134-136 forward only type(e).__name__.
- **Impact on the Co-Pilot:** - Schema and SQL details are exposed to any API caller, which helps attackers.
  - If the agent ever forwards FHIR error bodies to Claude or Langfuse, internal SQL (and possibly bound values) enters the LLM context and traces.
- **Recommendation:** - In the agent, keep passing only status code and exception type to the LLM (main.py:133-135 already does this). Never pass the response body.
  - In OpenEMR, patch ExceptionHandlerListener, dispatch.php and the AuthorizationController catch blocks to return a generic message plus a correlation id outside debug mode, and log the detail server-side.
- **Verifier correction:** - AuthorizationController line references were off: 622-625 should be 624-629, and 1411-1415 should be 1408-1416.
  - Statements use ? placeholders, so bound values are not included, although MySQL error text can echo values.
  - The agent already strips response bodies, so impact on this project is low (schema disclosure only).

#### SEC-10

**OIDC end-session endpoint: unsigned id_token_hint and open redirect** — *low*

- **Evidence:** - src/RestControllers/AuthorizationController.php:1462-1463: id_token_hint payload decoded with no signature check.
  - :1469-1475: unregistered post_logout_redirect_uri redirect when no trusted user exists.
  - :1481-1487: nonce comparison ('' === '' passes when neither side has a nonce), then deleteTrustedUserById.
  - :1488-1489: registered logout URIs are checked only on this second branch.
  - src/RestControllers/Subscriber/CORSListener.php:56-57 and 71-73: Origin reflected; :67 also sets Access-Control-Allow-Credentials true on preflight.
- **Impact on the Co-Pilot:** The EHR's own domain can be used as a phishing redirect aimed at clinicians (for example, a fake re-login page after the copilot logs out). A forged logout can also revoke a clinician's copilot session mid-visit, which is a nuisance-level denial of service during the 90-second window.
- **Recommendation:** - Verify the id_token_hint signature with oapublic.key before trusting aud and sub.
  - Redirect only to post_logout_redirect_uris registered for that client, on both branches.
  - For the agent, register exact redirect and logout URIs and do not rely on the end-session redirect.
  - Optionally restrict CORS to the agent's origin at the Railway or Apache layer.
- **Verifier correction:** - Minor line shifts only. A forged logout requires knowing the user uuid (sub) and client_id (aud).

#### SEC-M3

**SMART v2 granular scope constraints are enforced only for Condition, Observation and RelatedPerson** — *low*

- **Evidence:** - src/Common/Auth/OpenIDConnect/Entities/ScopeEntity.php:140-180: containsScope() compares context, resource, operation and permissions but ignores query constraints.
  - ScopeEntity.php:93-97: getScopeLookupKey is context/resource only.
  - src/Common/Http/HttpRestRequest.php:373-380 uses this for the AuthorizationListener.php:186-193 check.
  - Constraint filtering (ResourceConstraintFilterer) is referenced only in src/RestControllers/FHIR/FhirGenericRestController.php (grep getRequestRequiredScope|ResourceConstraintFilterer).
  - The generic controller is used only at routes :167-175 (Condition), :493-500 (Observation) and :715 (RelatedPerson).
- **Impact on the Co-Pilot:** If the team narrows the copilot to minimum-necessary data with granular scopes (for example DocumentReference.rs?category=clinical-note, or restricted DiagnosticReport or MedicationRequest categories), OpenEMR accepts the token but returns every record of that resource type. The consent screen and audit documentation would claim a narrower data scope than the agent actually receives.
- **Recommendation:** - Do not rely on granular (?category=) scopes for least privilege outside Condition and Observation.
  - Apply category filters in agent tool queries and document this server limitation in AUDIT.md.
  - Optionally port the controllers the agent uses to FhirGenericRestController so constraints are enforced.

### Performance

| ID | Severity | Finding | Verification |
|---|---|---|---|
| [PERF-1](#perf-1) | high | Per-request legacy bootstrap tax: globals.php, Laminas module bootstrap, and 2-3 new MySQL connections on every FHIR call | Confirmed with corrections |
| [PERF-2](#perf-2) | high | Default audit settings multiply synchronous writes per read (SELECT auditing, SHOW COLUMNS auditing, full PHI bundle stored twice in api_log) | Confirmed with corrections |
| [PERF-3](#perf-3) | high | MedicationRequest materializes a UNION over all patients before the patient filter, and has per-row N+1 organization and code lookups | Confirmed |
| [PERF-4](#perf-4) | high | No server-side paging, limits or sorting for clinical resources; whole patient histories return in one bundle | Confirmed |
| [PERF-11](#perf-11) | high | Agent loop design will exceed the 10 s p95 if tool rounds are LLM-driven; timeouts and client settings don't match the budget | Confirmed |
| [PERF-M2](#perf-m2) | high | Agent LLM cost grows each round and turn: unbounded history, full tool JSON resent, no prompt caching, no streaming | Found by verifier |
| [PERF-M3](#perf-m3) | high | Langfuse @observe captures tool outputs and chat responses (PHI) by default (cross-dimension: Compliance; small performance cost) | Found by verifier |
| [PERF-5](#perf-5) | medium | Observation and Condition run their sub-searches in series inside one request, and Observation builds 10 sub-services on every call | Confirmed with corrections |
| [PERF-6](#perf-6) | medium | Service constructors do UUID backfill and schema introspection on every GET, with row-by-row UPDATE storms after bulk imports | Confirmed |
| [PERF-7](#perf-7) | medium | Bearer-token validation and ACL checks cost about 10 DB queries plus an audit write per call, with no caching | Confirmed with corrections |
| [PERF-8](#perf-8) | medium | Index gaps and type mismatches on patient-scoped joins (appointments full scan, form_encounter.encounter, lists pid+type), plus an appointment N+1 | Confirmed with corrections |
| [PERF-10](#perf-10) | medium | Container concurrency and caching are unverified: PHP worker pool size, opcache sizing, no application cache, file sessions | Confirmed with corrections |
| [PERF-M1](#perf-m1) | medium | Custom-module bootstrap retry sleeps 5 s per retry (15 s per request) because of a unit bug in usleep | Found by verifier |
| [PERF-9](#perf-9) | low | /fhir/metadata is unauthenticated and builds a service for every route; the agent's readiness probe calls it | Confirmed with corrections |

#### PERF-1

**Per-request legacy bootstrap tax: globals.php, Laminas module bootstrap, and 2-3 new MySQL connections on every FHIR call** — *high*

- **Evidence:** src/RestControllers/Subscriber/SiteSetupListener.php:202 require_once globals.php. interface/globals.php:411 SHOW TABLES, :450-453 SELECT from globals (493 settings in library/globals.inc.php), :518 SET time_zone, :565 lang_languages, :736 existsTable('modules'), :748 new ModulesApplication. src/Core/ModulesApplication.php:41-72 (loadModules, bootstrap), :124 and :141 module queries; no cache key in interface/modules/zend_modules/config/application.config.php. library/sql.inc.php:60-61: persistence is decided before globals load. interface/globals.php:664-665 copies the pooling flag into the session, but API sessions are thrown away at terminate (SessionCleanupListener.php:26-28) and use_strict_mode=1 (SessionConfigurationBuilder.php:22), so the main connection is non-persistent. EventAuditLogger.php:44 createDbal($opts,false) is a separate, non-persistent audit connection. Gacl.php:139-140 createAdodb with detectConnectionPersistenceFromGlobalState() runs after globals are loaded, and 'enable_database_connection_pooling' defaults to '1' (library/globals.inc.php:2941-2944), so the GACL connection is normally PConnect and reused per FPM worker. File session with no read_and_close: SessionConfigurationBuilder.php:105-112.
- **Impact on the Co-Pilot:** Every FHIR call pays a floor of dozens of DB round trips plus TCP/auth (and TLS, if configured) handshakes, whether or not it returns data. A chart-open fan-out of 6-8 calls pays that floor 6-8 times and competes for the container's 1-2 vCPUs. Estimate (unmeasured): about 150-400 ms per call on a small DB. That is a large share of the 5 s p50 before any clinical query or LLM work, and parallelism will not remove it.
- **Recommendation:** Treat the call count as the budget. Collapse the agent's FHIR calls to one filtered prefetch at chart open and cache it in the agent process with a short TTL, keyed by user, patient and token. Measure the real floor with performance_schema statement digests and `hey` at concurrency 1/4/8 against the Railway deployment. On the OpenEMR side, consider enabling the Laminas config cache and making the API DB connections persistent, after checking the security and compliance impact.
- **Verifier correction:** The mechanism is real, but three points are overstated. (1) Only 2 connections are opened fresh per request (main ADODB and audit DBAL). The GACL connection is persistent by default because it is created after globals load with pooling=1. (2) The '6-8 call fan-out' is the planned agent, not the current one: agent/fhir.py:95 exposes only 3 tools (patient, allergies, meds), so a round sends at most 3 parallel calls today. (3) The 150-400 ms estimate is unmeasured. The LLM round trip (PERF-11) is the larger share of the latency budget, and parallel calls across FPM workers do not add their floors to wall-clock time. Severity: high, not critical.

#### PERF-2

**Default audit settings multiply synchronous writes per read (SELECT auditing, SHOW COLUMNS auditing, full PHI bundle stored twice in api_log)** — *high*

- **Evidence:** Defaults: library/globals.inc.php:2778 enable_auditlog '1', :2785 patient-record '1', :2811 security-administration '1', :2832 audit_events_query '1', :2845 http-request '1', :2893-2902 api_log_option '2'. library/ADODB_mysqli_log.php:26-51 Execute calls auditSQLEvent. EventAuditLogger.php:429-437 query type defaults to 'select'. QueryUtils.php:33-35 listTableFields runs fetchRecords(noLog=false), so SHOW COLUMNS FROM `lists` is audited as patient-record. EventAuditLogger.php:486-497 LOG_TABLES match. LogTablesSink.php:60,94,98 3 INSERTs (2 without api), :83 sha3 over both bodies. ApiResponseLoggerListener.php:84-85 request_body = response = full body. BearerTokenAuthorizationStrategy.php:316 newEvent (not gated by config; EventAuditLogger.php:187-218). globals.php:848-850 logHttpRequest. NEW: GACL queries go through ADODB_mysqli_log (DatabaseConnectionFactory requires ADODB_mysqli_log; Gacl.php:462 and :618 db->Execute), and they hit LOG_TABLES entries such as gacl_aro, gacl_acl and 'groups' (EventAuditLogger.php:148-172), so every ACL check also writes audit rows. ApiApplication.php:131-133 calls response->send() before kernel->terminate().
- **Impact on the Co-Pilot:** One FHIR search becomes about 10-20+ autocommit INSERTs, each with a redo-log flush on Railway volume storage. Large bundles such as vitals or labs are written to api_log twice. Write latency adds directly to per-call time (terminate-time work holds a PHP worker; whether it also blocks the client depends on whether the image runs FPM or mod_php, which needs verifying). Under a 20-patient day with repeated agent queries, log and api_log grow fast and hold a full copy of every PHI response the agent reads.
- **Recommendation:** Take this to Compliance and do not just switch auditing off. api_log_option=1 (minimal) removes the duplicated full-body PHI while keeping the log row. Decide whether SELECT-level auditing stays on or is replaced by one access event per FHIR request, which the agent's Langfuse or correlation-ID trail can supplement. Measure INSERT commits per request before and after. Longer term, batch the audit writes or make them asynchronous in one transaction.
- **Verifier correction:** The count is right or slightly low, and the auditor missed a source: every aclCheckCore runs acl_get_groups plus acl_query, twice for a non-superuser, and each is an audited SELECT (2 INSERTs each). The blocking claim needs refining. The SQL-audit, API-success and http-request INSERTs happen inline, before the response. The api_log write (the duplicated PHI body) runs at kernel.terminate after response->send(). Under PHP-FPM, Symfony's send() calls fastcgi_finish_request, so that write does not delay the client; under mod_php it may. As a performance issue this is high, not critical. The duplicated full-body PHI in api_log is a compliance problem and should be routed there.

#### PERF-3

**MedicationRequest materializes a UNION over all patients before the patient filter, and has per-row N+1 organization and code lookups** — *high*

- **Evidence:** src/Services/PrescriptionService.php:95-337 base SQL. UNION of prescriptions (with lists_medication derived at :194-204) and lists (:205-260), materialized as combined_prescriptions; WHERE appended outside at :73-77. issue_encounter GROUP BY over all patients :250-257. form_encounter derived join on encounter :314-320. Practitioner NPI filter :321-328, reporting_source NPI filter :329-337. addCoding per row :350, which does new CodeTypesService plus lookup_code_description (CodeTypesService.php:139-151 calls lookup_code_descriptions up to twice; custom/code_types.inc.php:776, with SHOW TABLES at :821 and SELECT at :874). lists_medication has indexes only on id, usage_category, request_intent and list_id; none on prescription_id (sql/database.sql:7730-7734). form_encounter keys: sql/database.sql:2058-2061. FhirMedicationRequestService.php:447-457 populateRequestor does `new FhirOrganizationService()` per row when pruuid is empty. FhirOrganizationService.php:57-67 builds Facility, Insurance and ProcedureProvider services, all BaseService with UUID backfill (InsuranceCompanyService.php:65, ProcedureProviderService.php:33, FacilityService.php:45). ALSO FhirMedicationRequestService.php:392-416 populateReported: when reporting_source_type is empty (it is only set via the NPI-filtered reporting_source join, PrescriptionService.php:332), every row calls getPrimaryBusinessEntityReference, which runs FacilityService::search (FacilityService.php:91-109, BaseService::search at :487, QueryUtils selectHelper using logged Execute). facility is in LOG_TABLES, so each call is also an audited SELECT.
- **Impact on the Co-Pilot:** Medications are the most safety-relevant data in the briefing and also the slowest call. Cost grows with total prescriptions in the whole practice, not with this patient's. Seed or demo users without NPIs send every medication row down a path of about 15-25 round trips. A patient with 12-15 meds could take seconds (inference, needs EXPLAIN ANALYZE and timing), enough by itself to push the briefing past 10 s p95.
- **Recommendation:** Measure with EXPLAIN ANALYZE on a Synthea-scale dataset. Short term: give provider users NPIs in the deployment data so the per-row organization path is skipped, and send `status=active` from the agent (it only filters after materialization, but it cuts payload and tokens). OpenEMR fix: push `patient_id = ?` / `pid = ?` into both UNION branches, memoize FhirOrganizationService and the primary business entity once per request, add indexes on form_encounter(encounter) and lists_medication(prescription_id), and batch code-description lookups.
- **Verifier correction:** The substance holds, with a few line numbers off by 3-4 (form_encounter join at 314-320, NPI filter at 321-328, addCoding at 350). The short-term fix of giving users NPIs does not remove the per-row organization cost: populateReported still runs one audited facility search per MedicationRequest row for ordinary records with no reporting source, whatever the NPI. It memoizes the service object (FhirMedicationRequestService.php:581-587) but not the result. The recommendation should be to memoize the primary-business-entity reference once per request. The whole-practice materialization is still inference until EXPLAIN ANALYZE; MySQL derived-condition pushdown does not apply because the filter is on the separate patient derived table.

#### PERF-4

**No server-side paging, limits or sorting for clinical resources; whole patient histories return in one bundle** — *high*

- **Evidence:** src/Common/Database/QueryPagination.php:20 DEFAULT_LIMIT = 0, :22 MAX_LIMIT = 200. src/Services/FHIR/Traits/ResourceServiceSearchTrait.php:53-66 moves _sort/_count/_offset into _config. src/Services/FHIR/FhirServiceBase.php:263-266 passes the config, :313-316 default searchForOpenEMRRecordsWithConfig ignores it. The only override is FhirPatientService.php:924. FhirResourcesService.php:39-44 'collection', total=count, self link only. FhirPatientRestController.php:742 uses the same createBundle, so no next link. FhirGenericRestController.php:105-128, FhirAllergyIntoleranceRestController.php:212-227. VitalsService.php:199 ORDER BY with no LIMIT. FhirObservationVitalsService.php:433-451 expands every code. FhirEncounterService.php:314-316 calls encounterService->search with no limit (EncounterService.php:321-329). Post-fetch filtering: FhirGenericRestController.php:113-119 canAccessResource, which leads to src/FHIR/SMART/ResourceConstraintFilterer.php:26-55. docker/release/php.ini:430 memory_limit 512M.
- **Impact on the Co-Pilot:** The agent cannot ask the server for the last 5 labs or the most recent vitals. It gets everything, unordered, and _count is silently ignored, so a naive tool that trusts _count will pass hundreds of Observations to Claude. That costs input tokens and LLM latency, PHP memory and serialization time, api_log bytes (stored twice, PHI), and network transfer. Patient _count truncates with no next link, so an agent could wrongly treat a partial set as complete.
- **Recommendation:** The agent must always send `date=ge...` for Observation, Encounter and Appointment, and `category` for Observation and Condition. Do not rely on _count or _sort. Sort, deduplicate and trim to the top N on the agent side before prompting, and log in Langfuse how many records were dropped. Document in AUDIT.md that FHIR bundles are complete, unpaginated collections. For OpenEMR, implement searchForOpenEMRRecordsWithConfig (LIMIT/ORDER BY) for Observation, MedicationRequest, Condition, Encounter and Appointment.
- **Verifier correction:** Minor corrections only. Encounter is ordered (EncounterService.php:322 `ORDER BY fe.eid DESC`) but has no limit. The allergy, prescription, condition and appointment SQL has no ORDER BY, as stated. The ResourceConstraintFilterer lines are 26-55, not 18-45. Today the agent calls only AllergyIntolerance and MedicationRequest (agent/fhir.py:67,81), so the Observation-volume impact applies to planned tools.

#### PERF-11

**Agent loop design will exceed the 10 s p95 if tool rounds are LLM-driven; timeouts and client settings don't match the budget** — *high*

- **Evidence:** agent/main.py:24 MAX_TOOL_ROUNDS = 5. :156-170 loop: one messages.parse per round (:158-161, max_tokens=4000, non-streaming), then asyncio.gather of the tools (:169). No overall deadline. :53 httpx.AsyncClient(timeout=15) with a shared default cookie jar. agent/fhir.py:67 and :81 send only {patient}. :53-54 read only bundle['entry']. agent/fhir.py:95 exposes only 3 tools. Session cookie behavior: SessionConfigurationBuilder.php:22 use_strict_mode true, php.ini:1313, SessionCleanupListener.php:26-28 invalidate at terminate.
- **Impact on the Co-Pilot:** Each extra tool round costs a full Claude call (about 1.5-4 s, estimate) plus another wave of expensive FHIR calls. A common pattern of tools, tools, then answer already overruns the p95. A single slow FHIR call (MedicationRequest or unfiltered Observation) can hold the whole round for up to 15 s with no partial answer. The shared cookie jar is untidy and can cause session-lock waits when requests overlap.
- **Recommendation:** At SMART launch, prefetch one fixed set of calls in parallel: Patient, active meds, allergies, problem-list Conditions, labs from the last 12 months, vitals from the last 6 months, recent Encounters, upcoming Appointments. Cache for about 60-120 s in memory, keyed by user, patient and token hash, and answer the first briefing in a single LLM round. Keep tool rounds for follow-ups, capped at 1-2. Set per-call `httpx.Timeout(connect=1, read=3)` and an overall `asyncio.wait_for` deadline, returning a partial answer that names the missing sections. Use `cookies=None` and `limits` sized to the verified PHP pool. Add Langfuse spans that split FHIR time from LLM time.
- **Verifier correction:** This is under-rated. The current design needs at least 2 non-streaming Claude calls per briefing (tool_use, then the structured answer), plus FHIR time, before anything is shown. That is the largest single threat to a 5 s p50 and should be high. One softening: with use_strict_mode=1 and the session invalidated at terminate, stale apiOpenEMR cookies are rejected and replaced with fresh ids. Session-file lock waits from the shared cookie jar therefore happen only when a request reuses the id of a request still in flight. It is still worth setting cookies off.

#### PERF-M2

**Agent LLM cost grows each round and turn: unbounded history, full tool JSON resent, no prompt caching, no streaming** — *high*

- **Evidence:** agent/main.py:124 `SESSIONS: Dict[str, dict] = {}` has no eviction or TTL. :144 setdefault, :153 appends the user message, :164 appends the full assistant content, :133 `json.dumps(records)` puts full tool results into messages, and :170 appends them. :158-161 `messages.parse(model=MODEL, max_tokens=4000, system=SYSTEM, tools=TOOL_SPECS, messages=messages, output_format=Briefing)` sets no cache_control on system, tools or history and does not stream. agent/verify.py:17-19 Briefing makes the model generate all claims (each with quoted_value) before `answer`.
- **Impact on the Co-Pilot:** Every follow-up question in a session resends the entire prior transcript, including all earlier tool results, so input tokens and time-to-first-token grow with each turn during the 90-second window. The second round of every briefing already resends round one's tool output without a cache hit. With no streaming and claims generated before the answer, the physician sees nothing until the full structured output is done. SESSIONS also grows without bound in the agent process (memory, and PHI kept in RAM).
- **Recommendation:** Add cache_control breakpoints on system plus tools and on the last tool-result block. Store compact, deduplicated tool records per session (keyed by source_id) instead of resending raw history, and cap the history turns. Stream the response, or return the answer first and verify claims after. Evict sessions on a TTL of a few minutes. Log input_tokens, cache_read_input_tokens and TTFT per round in Langfuse to confirm the effect.

#### PERF-M3

**Langfuse @observe captures tool outputs and chat responses (PHI) by default (cross-dimension: Compliance; small performance cost)** — *high*

- **Evidence:** agent/fhir.py:57, :64, :78 `@observe(as_type="tool", capture_input=False)` disables only input capture, so output (PatientOut name, birth_date, gender; allergies; medications) is captured by default. agent/main.py:141 `@observe(name="chat", capture_input=False)` captures the ChatResponse output (answer and claims with quoted_value). :150 sends patient_id in trace metadata. agent/.env.example sets LANGFUSE_HOST=https://us.cloud.langfuse.com and has no setting that disables IO capture.
- **Impact on the Co-Pilot:** Every briefing sends identifiable PHI to a third-party tracing SaaS. The project assumes a BAA only for Anthropic. Serializing and uploading these payloads also adds background CPU and network work per request, a minor performance cost.
- **Recommendation:** Route to the Compliance section. Set capture_output=False on the tool and chat observers, or apply a masking function. Log only counts, resource ids, latencies and token usage. Alternatively, confirm a Langfuse BAA or self-host Langfuse before any real PHI flows.

#### PERF-5

**Observation and Condition run their sub-searches in series inside one request, and Observation builds 10 sub-services on every call** — *medium*

- **Evidence:** src/Services/FHIR/FhirObservationService.php:60-74 constructs 10 sub-service objects; :147-151 falls back to all mapped services; src/Services/FHIR/Traits/MappedServiceTrait.php:66-79 sequential foreach. FhirConditionService.php:46-53 (3 sub-services plus ConditionService), :107-111 fallback. Eager DB-touching constructors: FhirObservationVitalsService.php:337-338 (VitalsService, UUID backfill at VitalsService.php:53), FhirObservationSocialHistoryService.php:80-81 (SocialHistoryService.php:37 backfill), FhirObservationObservationFormService.php:40-41 (ObservationService extends BaseService), FhirObservationHistorySdohService.php:84-85 (HistorySdohService extends BaseService). Lazy, not in the constructor: FhirObservationLaboratoryService.php:63-74 (ProcedureService is created only in getProcedureService; the constructor line is commented out at :66), FhirObservationPatientService.php:103, FhirObservationEmployerService.php:109/122, FhirObservationAdvanceDirectiveService.php:158.
- **Impact on the Co-Pilot:** An unfiltered `Observation?patient=X` runs about 10 back-end searches one after another in one PHP worker. It is typically the slowest call in the fan-out and sets the fan-out's tail latency. Even filtered calls pay the constructor cost for all 10 sub-services.
- **Recommendation:** The agent must split Observation into separate parallel calls, `category=laboratory&date=ge{12mo}` and `category=vital-signs&date=ge{6mo}` (optionally with explicit LOINC codes), and never send Observation without a category. Use `category=problem-list-item` for Condition in the briefing. For OpenEMR, create mapped sub-services lazily, only for the categories selected.
- **Verifier correction:** The auditor's example FhirObservationLaboratoryService.php:72 `new ProcedureService()` is not in the constructor; it is lazy. Only 4 of the 10 Observation sub-services build a BaseService (SHOW TABLES, audited SHOW COLUMNS, auto-increment SHOW COLUMNS, UUID backfill) at construction, so filtered calls pay about 4 service bootstraps, not 10. The serial unfiltered sub-search is real. The current agent has no Observation or Condition tool (agent/fhir.py:95), so this is a design constraint for planned tools rather than a live bottleneck. Severity: medium.

#### PERF-6

**Service constructors do UUID backfill and schema introspection on every GET, with row-by-row UPDATE storms after bulk imports** — *medium*

- **Evidence:** src/Services/BaseService.php:69-70 constructor. QueryUtils.php:31-41 listTableFields also calls escapeTableName, which runs `SHOW TABLES` (QueryUtils.php:50) before the audited SHOW COLUMNS. BaseService.php:313-325 getAutoIncrements runs SHOW COLUMNS ... WHERE extra. So each service costs 3 introspection queries plus 1 audit write, not '2 SHOW COLUMNS'. UuidRegistry.php:229-234 createMissingUuidsForTables; :295-325 createMissingUuids (BeginTrans, loop, CommitTrans); :432-437 count(*) via fetchRecordsNoLog; :413-429 per-row `UPDATE ... SET uuid = ? WHERE id = ?`. The id SELECT at :421 uses logged fetchRecords, so it is audited during backfill. Constructor calls: AllergyIntoleranceService.php:39-40, PrescriptionService.php:41-42, EncounterService.php:67-68, AppointmentService.php:55-56, ConditionService.php:38, VitalsService.php:53, ProcedureService.php:46, FacilityService.php:45. 31 call sites across 30 files in src. UuidRegistry.php:236-241 docblock itself warns against 'letting an authenticated request trigger a whole-table backfill'. php.ini:404 max_execution_time 60. populateAllMissingUuids at UuidRegistry.php:128.
- **Impact on the Co-Pilot:** In steady state this adds about 3 round trips per table per service to every call, roughly 12-30 extra round trips before the real query. After any bulk load without UUIDs (Synthea or SQL import to Railway), the first agent read runs an UPDATE backfill of the whole table inside a GET. It can take seconds to minutes, can hit max_execution_time=60 (docker/release/php.ini:404), and parallel calls race on the same rows. That shows up as random first-request timeouts during demos.
- **Recommendation:** Add a deployment step that runs UuidRegistry::populateAllMissingUuids (UuidRegistry.php:128) once after every data import, before agent traffic. Add a smoke check that the missing-UUID counts are 0. For OpenEMR, move the backfill out of constructors (or guard it with a static flag per process) and cache the table-field lists.
- **Verifier correction:** Confirmed, with small line offsets (createMissingUuids at 295-325, UPDATE loop at 413-429). The steady-state cost is small on a private network: about 3 round trips per table plus 3 introspection queries per service. It overlaps with PERF-1's floor, so severity is lower. The real risk is conditional: the first GET after a bulk SQL import without UUIDs. Added inference: parallel first requests can both backfill the same rows, overwriting UUIDs, so resource ids can change between calls and break the agent's source_id citation check (agent/verify.py:30). Severity: medium, but the post-import populateAllMissingUuids step should stay in the runbook.

#### PERF-7

**Bearer-token validation and ACL checks cost about 10 DB queries plus an audit write per call, with no caching** — *medium*

- **Evidence:** BearerTokenAuthorizationStrategy.php:161 isAccessTokenRevokedInDatabase leads to AccessTokenRepository.php:108. :169 leads to TrustedUserService.php:36 (oauth_trusted_user keys at sql/database.sql:14143-14145). :186-188 leads to UuidUserAccount.php:65,100-101. :251 getAuthGroupForUser. :259-265 plus :461 getTokenByToken. :292 leads to AccessTokenRepository.php:56. :316 newEvent (2 INSERTs). RestConfig.php:187 leads to AclMain.php:174 (admin/super recursion) and :181 acl_query. Each acl_query runs acl_get_groups (Gacl.php:317, query at :618) plus the main query (Gacl.php:462), both through the audited ADODB_mysqli_log driver. Gacl.php:68 _caching = FALSE. Gacl connection at Gacl.php:139-140 is persistent when pooling is on (default '1', globals.inc.php:2941-2944). Routes: apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php:79, :102, :303. AuthorizationController.php:110 PT1H.
- **Impact on the Co-Pilot:** Authorization is not free on repeated calls: a clinician (not a superuser) pays 2 GACL join queries across 7 tables on a third connection, plus about 8 token and user lookups, for every FHIR call. It is a smaller share than audit writes or bad queries, but it is part of the fixed floor multiplied by the fan-out.
- **Recommendation:** Don't try to optimize JWT verification; the cost is in the DB lookups. On the agent side, reduce the number of calls (prefetch plus cache) and handle refresh before the 1-hour token expires so a mid-visit 401 does not force a retry. For OpenEMR, consider memoizing token and user lookups per process with a TTL of a few seconds, and GACL results per user (enable GACL caching), with the revocation-latency tradeoff documented for Security.
- **Verifier correction:** The GACL cost is understated. Each aclCheckCore is 2 queries (group lookup plus acl_query), and a non-superuser pays it twice (the admin/super check, then the section), so 4 GACL queries. Each is also an audited SELECT (the gacl_* and 'groups' entries in LOG_TABLES), adding about 8 audit INSERTs per call. The GACL connection is usually persistent, not a fresh 'third connection'. The token and user lookups themselves use NoLog queries. Medium severity stands.

#### PERF-8

**Index gaps and type mismatches on patient-scoped joins (appointments full scan, form_encounter.encounter, lists pid+type), plus an appointment N+1** — *medium*

- **Evidence:** sql/database.sql:8266 `pc_pid varchar(11)`, keys :8301-8304 (basic_event, pc_eventDate, uuid). AppointmentService.php:187 `pd ON pd.pid = pce.pc_pid`. FhirAppointmentService.php:156 leads to AppointmentService.php:679-682 (logged fetchRecords; table in LOG_TABLES at EventAuditLogger.php:143). form_encounter keys sql/database.sql:2058-2061; joined on encounter alone at PrescriptionService.php:314-320, ProcedureService.php:240-246, ConditionService.php:69-70. issue_encounter has only UNIQUE(pid,list_id,encounter) (sql/database.sql:3448) but ConditionService.php:69 joins on list_id alone. lists keys sql/database.sql:7709-7710. users has no username key (sql/database.sql:9853-9855); joined at AllergyIntoleranceService.php:91, VitalsService.php:178, ConditionService.php:78. EncounterService.php:265 list_options join without list_id; PK (list_id, option_id) at sql/database.sql:3904.
- **Impact on the Co-Pilot:** Demo data hides these problems. At real practice volume, where the calendar holds every provider's schedule for years and form_encounter holds every visit, the Appointment, MedicationRequest, labs and Condition calls scan whole tables on every request. The tail latency grows with practice size, not patient size, which is exactly the p95 the agent is judged on. The class join without list_id can also duplicate Encounter rows, which is a data-quality risk.
- **Recommendation:** Record in AUDIT.md and verify with EXPLAIN ANALYZE on seeded data. Proposed indexes: form_encounter(encounter), openemr_postcalendar_events(pc_pid, pc_eventDate) with a fix or cast for pc_pid, lists(pid, type), lists_medication(prescription_id), users(username). The agent should always send `date=ge{today}` on Appointment (pc_eventDate is indexed, but the pid join still scans).
- **Verifier correction:** The important gaps are pc_pid (varchar, unindexed, joined to int pid) and form_encounter.encounter; issue_encounter(list_id), which is not a leading key, should be added. Two items are overstated: lists(pid,type), because per-patient lookups already use the pid index and a patient has few rows, and users(username), because users is a small staff and address-book table. The Encounter class-join duplication is theoretical with default data: option_ids such as 'AMB' exist only in _ActEncounterCode (sql/database.sql:5766). Some line numbers are off by 1-3. Appointment, Encounter and Condition are not called by the current agent.

#### PERF-10

**Container concurrency and caching are unverified: PHP worker pool size, opcache sizing, no application cache, file sessions** — *medium*

- **Evidence:** docker/release/openemr.conf:212-216 SetHandler proxy:fcgi://127.0.0.1:9000. docker/release/Dockerfile:88-126 installs php-apache2 and php-fpm; Dockerfile:253 copies openemr.conf. docker/release/openemr.sh:964 only runs `exec /usr/sbin/httpd -D FOREGROUND`; nothing in docker/release starts php-fpm or sizes a pool (grep for 'fpm' and 'pm.' in openemr.sh and Dockerfile finds only package lines). docker/binary/php-fpm.conf:14-19 pm.max_children=50 (a different image). docker/release/php.ini:1679 opcache.enable=1, :1685/:1692/:1705 sizing commented out, :430 512M, :404 60 s, :1275 files, :1313 use_strict_mode=1. openemr.sh:426-460 Redis only if REDIS_SERVER. composer.json:118 symfony/cache with no usage in src, library or interface. DatabaseConnectionOptions.php:136-161 mysql-ca detection. 4,481 PHP files outside vendor, 918 under src/FHIR (vendor is not in the repo).
- **Impact on the Co-Pilot:** The real ceiling on parallel fan-out is the number of PHP workers in one Railway container. If the running 8.5.0 image uses a small default FPM pool (Alpine's default is pm.max_children=5, inference) or low prefork limits, 6-8 parallel agent calls plus the clinician's own OpenEMR UI traffic will queue, and p95 will be set by queueing rather than query time. The 512M per worker must fit the Railway memory plan. Opcache that is too small for the file count causes restarts and CPU spikes.
- **Recommendation:** Verify in the running container: `ps aux | grep -E 'php-fpm|httpd'`, `httpd -M | grep -E 'mpm|php|proxy_fcgi'`, the effective pm.max_children or MaxRequestWorkers, `opcache_get_status()` (full or restart counts), and whether mysql-ca is present. Size the pool to Railway memory (large bundles use about 100-200M per worker, inference). Cap the agent's httpx concurrency at or below the pool size minus UI headroom. Record the numbers in AUDIT.md.
- **Verifier correction:** The evidence in the repo is internally inconsistent, which strengthens the 'unverified' framing. The release openemr.conf proxies PHP to FPM on :9000, but the release entrypoint never starts php-fpm, so these files cannot show how the running openemr/openemr:8.5.0 image serves PHP (FPM pool or mod_php prefork). It must be checked in the container. The '+vendor' opcache file count cannot be checked from the repo. The Alpine default pm.max_children=5 is inference. Medium severity stands.

#### PERF-M1

**Custom-module bootstrap retry sleeps 5 s per retry (15 s per request) because of a unit bug in usleep** — *medium*

- **Evidence:** src/Core/ModulesApplication.php:146 `if ($this->isFileReadableWithRetry($modulePath, 3, 50))`; :166-176 `usleep($wait * 100000); // Wait for a x milliseconds`. 50 * 100000 µs = 5 s per retry, times 3 retries = 15 s. It runs on every request, including every FHIR call, because globals.php:748 builds ModulesApplication, whose constructor calls bootstrapCustomModules (ModulesApplication.php:71, query at :141 `WHERE mod_active = 1 AND type != 1`). Default install data registers only type=1 Laminas modules (sql/database.sql:7814-7818), so the bug is latent until a custom module is enabled.
- **Impact on the Co-Pilot:** If any active custom module's openemr.bootstrap.php is unreadable (for example a module enabled in the Module Manager whose files are missing after an image or version change, or a permissions problem on Railway), every FHIR call stalls 15 s per such module before it runs. That exceeds the agent's 10 s p95 and its 15 s httpx timeout, and appears only as intermittent timeouts plus error_log lines. The modules table persists in MySQL across container redeploys, while module code comes from the image.
- **Recommendation:** Add to AUDIT.md as a latent tail-latency hazard. Check deployment health with `SELECT mod_name, mod_directory FROM modules WHERE mod_active=1 AND type!=1` and confirm each bootstrap file is readable by the web user. Look for 'Custom module bootstrap file ... is not readable' in the logs. The OpenEMR fix is one line: usleep($wait * 1000).

#### PERF-9

**/fhir/metadata is unauthenticated and builds a service for every route; the agent's readiness probe calls it** — *low*

- **Evidence:** src/RestControllers/Subscriber/AuthorizationListener.php:95 addSkipRoute('/fhir/metadata'). apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php:817-819. src/RestControllers/RestControllerHelper.php:484-509 `new $serviceClass()` per route key (71 FHIR routes). agent/main.py:79-103 /ready and :90-91 GET metadata. The response is JSON, so with api_log_option=2 the full CapabilityStatement is also written twice to api_log (ApiResponseLoggerListener.php:52-86); unauthenticated rows carry user_id 0.
- **Impact on the Co-Pilot:** Each readiness probe, possibly every few seconds from Railway, triggers one of the most expensive requests OpenEMR serves, adding steady DB and audit load that competes with clinician traffic. Because it is unauthenticated, it is also a cheap amplification path (flag for Security).
- **Recommendation:** Point `/ready` at `/fhir/.well-known/smart-configuration` or a lightweight TCP/HTTP check, or cache the CapabilityStatement in the agent at startup. For OpenEMR, cache the metadata response and consider rate-limiting unauthenticated routes at the edge.
- **Verifier correction:** The cost of the metadata request is real. The claim that it is probed 'possibly every few seconds from Railway' has no evidence: the agent/ directory has no railway.json or railway.toml configuring /ready as a healthcheck, and Railway healthchecks run at deploy time, not continuously (external knowledge, inference). With no continuous probe the impact on clinician traffic is small. The unauthenticated amplification (DB introspection plus a large api_log write per anonymous request) belongs to Security. Severity: low for performance.

### Architecture

| ID | Severity | Finding | Verification |
|---|---|---|---|
| [ARCH-1](#arch-1) | high | sites/ volume is the only record that OpenEMR is configured and the only copy of the drive encryption keys; losing it can wipe the schema and break OAuth | Confirmed with corrections |
| [ARCH-2](#arch-2) | high | Unsupported FHIR search parameters return HTTP 200 with an empty Bundle instead of a 400 | Confirmed |
| [ARCH-3](#arch-3) | high | Scope model: user/ scopes are ACL-checked but not bound to a patient; patient/ scopes are bound but skip ACL checks | Confirmed |
| [ARCH-6](#arch-6) | high | The FHIR mapping layer is lossy and inconsistent: allergy names end up only in narrative, inactive allergies look active, and meds with any end date are 'completed' | Confirmed |
| [ARCH-M1](#arch-m1) | high | Visit notes (SOAP, dictation, LBF/CAMOS) are not in the FHIR API; DocumentReference exposes only the Clinical Notes form | Found by verifier |
| [ARCH-M2](#arch-m2) | high | Condition?category=problem-list-item excludes any problem linked to an encounter; those appear once per encounter as encounter-diagnosis | Found by verifier |
| [ARCH-4](#arch-4) | medium | Encounter sensitivity ACL is enforced in the UI and update path only; FHIR search returns sensitive encounters | Confirmed with corrections |
| [ARCH-5](#arch-5) | medium | EHR launch: patient comes from session pid, app runs in an iframe modal, and a login screen appears unless the client is confidential, admin-enabled, and has both skip flags set | Confirmed with corrections |
| [ARCH-7](#arch-7) | medium | Per-request API overhead, with no batching or pagination, drives briefing latency | Confirmed |
| [ARCH-8](#arch-8) | medium | SMART-critical globals are off or empty by default, and the Docker env-to-globals sync is fragile | Confirmed with corrections |
| [ARCH-9](#arch-9) | medium | Deployed image code is not this repo, and only sites/ persists, so any module or route change needs a derived image | Confirmed |
| [ARCH-11](#arch-11) | medium | API audit trail records patient_id=0 for user-role FHIR reads and stores full PHI response bodies by default | Confirmed |
| [ARCH-M3](#arch-m3) | medium | Default SQL SELECT auditing writes log and log_comment_encrypt rows for every patient-table query in each FHIR call, with patient id 0 for API users | Found by verifier |
| [ARCH-10](#arch-10) | low | Service-layer change events miss major write paths (eRx, HL7 labs, CDA import), so OpenEMR events can't drive agent cache invalidation | Confirmed |
| [ARCH-12](#arch-12) | low | FHIR ids (UUIDs) are backfilled lazily inside GET requests, so citation ids are not guaranteed stable across restores or imports | Confirmed with corrections |

#### ARCH-1

**sites/ volume is the only record that OpenEMR is configured and the only copy of the drive encryption keys; losing it can wipe the schema and break OAuth** — *high*

- **Evidence:** The wipe path checks out. docker/release/openemr.sh:708 reads $config. openemr.sh:744-748 runs auto-configure when CONFIG=0, MYSQL_ROOT_PASS is set and MANUAL_SETUP!=yes. sites/default/sqlconf.php:24 has `$config = 0;`. library/classes/Installer.class.php:1526-1552 skips create-db when the openemr user can already connect, then calls load_dumpfiles() at 1561. load_dumpfiles (452-466) loads main_sql = sql/database.sql (line 119), which has 282 `DROP TABLE IF EXISTS` (7670 lists, 8333 patient_data). CryptoGen.php:448-453 silently creates a new drive key when the file is missing (createDriveKey 473-497). The OAuth secrets do NOT depend on drive keys: OAuth2KeyConfig.php:114 and 133 use decryptFromDatabase, i.e. the DB key in the `keys` table. ClientRepository.php:93 and 200 encrypt and decrypt client_secret with encryptForDatabase/decryptFromDatabase. What does live only on sites/ is the RSA keypair at OAuth2KeyConfig.php:63 (documents/certificates/oaprivate.key). If it is missing, verifyKeys() returns false and createOrRecreateKeys() (194-255) deletes and regenerates oauth2key, the passphrase and the keypair. docker/production/docker-compose.yml:33-34 mounts only logvolume01:/var/log and sitevolume:/.../openemr/sites.
- **Impact on the Co-Pilot:** If the Railway sites volume is missing or detached on redeploy, the container thinks it is a fresh install and reloads the schema into the existing MySQL. That destroys all patient data the agent reads. Even without a data wipe, regenerated drive keys make keys.oauth2key, the OAuth passphrase, and the Co-Pilot's encrypted client_secret undecryptable, so every SMART launch and token exchange fails and the agent is down.
- **Recommendation:** Treat the sites/ volume as stateful with the same importance as the DB. Mount /var/www/localhost/htdocs/openemr/sites on a Railway volume. After first install, set MANUAL_SETUP=yes so auto-configure can never run again. Back up sites/ and the DB together as one snapshot. Add a deploy smoke check that sqlconf.php has $config=1 and that /apis/default/fhir/.well-known/smart-configuration returns 200 before routing traffic.
- **Verifier correction:** The claim that regenerated drive keys make keys.oauth2key, the OAuth passphrase and the Co-Pilot client_secret undecryptable is wrong. All three are encrypted with the database key in the `keys` table, so they survive loss of sites/. The real non-wipe impact of losing sites/ is different: the OAuth RSA keypair is missing, so OAuth2KeyConfig::createOrRecreateKeys regenerates oauth2key, the passphrase and the keypair. Every issued access and refresh token becomes invalid and the JWKS changes, but the client registration keeps working. Drive-key data (e.g. SMART launch tokens, which use encryptStandard with the default Drive source, and encrypted documents) is lost. The schema-wipe path is real but needs an operational mistake: a detached or missing volume while MYSQL_ROOT_PASS is still set. Lowered to high because the deployment facts already call for a persistent sites/ volume. MANUAL_SETUP=yes after first install is still the right guard.

#### ARCH-2

**Unsupported FHIR search parameters return HTTP 200 with an empty Bundle instead of a 400** — *high*

- **Evidence:** ResourceServiceSearchTrait.php:131 throws SearchFieldException for unknown fields. Only _sort, _count and _offset are special-cased (lines 53-65). FhirServiceBase.php:288-293 catches the exception and only calls setValidationMessages. FhirAllergyIntoleranceRestController.php:212-228 iterates getData() and always calls responseHandler(..., 200). FhirGenericRestController.php:104-114 returns the invalid result unchanged and 121-145 builds a 200 bundle; routes send Condition (routes file 167-168) and Observation (493-494) through it. FhirAllergyIntoleranceService.php:59-66 supports only patient, _id and _lastUpdated. FhirServiceBase.php:313-316 base searchForOpenEMRRecordsWithConfig ignores the config, and grep shows only FhirPatientService overrides it.
- **Impact on the Co-Pilot:** A natural tool call like AllergyIntolerance?patient=X&clinical-status=active returns {total:0}. The LLM will confidently report 'no known allergies', which is a patient-safety failure that looks like normal behavior and can't be detected downstream. Likewise, _count and _sort are silently ignored, so 'last 3 encounters' returns every encounter unsorted.
- **Recommendation:** Hard-code a per-resource allowlist of search params in the agent's FHIR tools, generated from and checked against the live /fhir/metadata CapabilityStatement. Reject any other param before sending. Do status, date-window and sort filtering client-side. In prompts and output, phrase an empty result as 'none recorded in OpenEMR', never 'none'. Add a contract test that sends a bogus param and fails if the result is 200 with empty data.
- **Verifier correction:** Minor line drift only: the generic controller's getAllProcessingResult is at 104-114 and getAll at 121-145.

#### ARCH-3

**Scope model: user/ scopes are ACL-checked but not bound to a patient; patient/ scopes are bound but skip ACL checks** — *high*

- **Evidence:** apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php:73-81: the patient-request branch passes getPatientUUIDString() with no ACL check; the else branch calls request_authorization_check(patients, med) and getAll without a patient bind. HttpRestRequest.php:421-424: a user/ scope overwrites the patient context. HttpRestRouteHandler.php:64-66 sets patientRequest only when the context is 'patient'. BearerTokenAuthorizationStrategy.php:479-485: checkUserHasAccessToPatient returns true. AuthorizationListener.php:143-150 requires a patient UUID only for patient requests. agent/main.py:107, 124-128 and 145-147 take patient_id from the request body and compare tool input against that same value.
- **Impact on the Co-Pilot:** With user/*.rs, OpenEMR lets the token read any patient's data. The launch 'patient' is only a hint in the token response. A prompt-injected or buggy tool call, or a tampered browser request, could pull another patient's PHI and the server would not stop it. With patient/*.rs, the binding is enforced, but OpenEMR's gacl role checks (e.g., a front-desk user without patients/med) are bypassed.
- **Recommendation:** Use user/*.rs so gacl still applies. Enforce patient pinning in the agent backend: after the token exchange, store the token-response `patient` (and `encounter`) server-side keyed by session. Never accept patient_id from the browser. Reject any FHIR call whose patient param, _id, or reference doesn't resolve to that patient. Log every rejection.

#### ARCH-6

**The FHIR mapping layer is lossy and inconsistent: allergy names end up only in narrative, inactive allergies look active, and meds with any end date are 'completed'** — *high*

- **Evidence:** FhirAllergyIntoleranceService.php:216-221: with no diagnosis, code = createDataAbsentUnknownCodeableConcept() (UtilsService.php:326-335, display 'Unknown') and the title goes only to text narrative. Lines 117-122: clinicalStatus = active when enddate is null, resolved when outcome=1 and enddate is set, else inactive (so any end date, even a future one, is non-active). Line 135 always sets category 'medication'. AllergyIntoleranceService.php has no activity filter in search (activity appears only on insert, line 272), whereas src/Services/FHIR/Condition/FhirConditionProblemListItemService.php:123 filters activity=1. PrescriptionService.php:181 and 235 map any non-null end_date with active=1 to 'completed'; the UNION is at 149-260. sql/database.sql:10493-10513 procedure_result has no patient column and result/range/abnormal are varchar. agent/fhir.py:40-44 _text returns coding.display ('Unknown') and 66-76 ignores text.div.
- **Impact on the Co-Pilot:** Free-text allergies, the common case when entered through the UI, come back as substance 'Unknown'. Deactivated allergies without an end date show as active. A 90-day prescription with a future end date is reported as 'completed', so the agent may tell the PCP a current med was stopped. Food and environmental allergies are labeled medication allergies. Every one of these produces confident, citation-backed wrong statements.
- **Recommendation:** Build a normalization layer in the agent's tools; don't pass raw FHIR to the LLM. Substance = code.text, else a non-data-absent coding.display, else the tag-stripped text.div. Compute medication status from authoredOn and dispenseRequest/validity dates against today, not from `status`. Mark allergy status as unverified. Keep a source field (prescriptions vs medication list) in citations. Add fixture tests seeded with uncoded allergies and future-end-date prescriptions.
- **Verifier correction:** The file path is src/Services/FHIR/Condition/FhirConditionProblemListItemService.php. One nuance: the issue UI marks an allergy inactive by setting an end date (interface/patient_file/summary/add_edit_issue.php:516), so 'deactivated without an end date shows active' mainly comes from non-UI paths (imports, activity=0 edits). The bigger UI-path error is that any end date, including a future one, makes an allergy non-active.

#### ARCH-M1

**Visit notes (SOAP, dictation, LBF/CAMOS) are not in the FHIR API; DocumentReference exposes only the Clinical Notes form** — *high*

- **Evidence:** src/Services/FHIR/DocumentReference/FhirClinicalNotesService.php:125-128 builds attachments only from ClinicalNotesService (src/Services/ClinicalNotesService.php:25 TABLE_NAME = "form_clinical_notes"), and 208-213 further limits to notes with no clinical_notes_category. SOAP notes are only on the non-FHIR standard API: apis/routes/_rest_routes_standard.inc.php:133 `GET /api/patient/:pid/encounter/:eid/soap_note` (EncounterService.php:619, 637 join form_soap). No src/Services/FHIR service reads form_soap, form_dictation, CAMOS or LBF tables, although interface/forms/ ships soap, dictation, CAMOS, LBF, clinic_note and note. The only narrative on FHIR Encounter is reasonCode.text from form_encounter.reason (FhirEncounterService.php:229-238).
- **Impact on the Co-Pilot:** The PCP's main between-rooms question is 'what happened last visit / what was the plan'. If the clinic documents in SOAP, dictation or LBF forms (the stock OpenEMR workflow), a FHIR-only agent sees no visit narrative, only the encounter reason. It will either say 'no notes' or build a plan from coded lists, and the gap looks like an empty chart rather than a coverage limit.
- **Recommendation:** Find out which note forms the seeded or demo clinic actually uses. For the MVP, either standardize documentation on the Clinical Notes form (exposed as DocumentReference with category clinical-note) or state in the UI that SOAP and dictation notes are out of scope. Using the standard /api soap_note route would break the FHIR-only constraint and require rest_api=1 plus api: scopes, so record that as an explicit decision.

#### ARCH-M2

**Condition?category=problem-list-item excludes any problem linked to an encounter; those appear once per encounter as encounter-diagnosis** — *high*

- **Evidence:** src/Services/FHIR/Condition/FhirConditionProblemListItemService.php:122-127 filters type=medical_problem, activity=1 and `list_id` MISSING, using a LEFT JOIN on issue_encounter (lines 177-182), which drops every problem that has any issue_encounter row. src/Services/FHIR/FhirConditionService.php:49-51 merges EncounterDiagnosis, ProblemListItem and HealthConcern. FhirConditionEncounterDiagnosisService.php:105-162 returns rows keyed by issue_encounter.uuid (one per linked encounter) with no activity filter. The UI links issues to encounters in interface/patient_file/summary/add_edit_issue.php:288-292 (linkIssueToEncounter) when an issue is added from within an encounter.
- **Impact on the Co-Pilot:** A natural 'active problem list' call (Condition?patient=X&category=problem-list-item) silently leaves out chronic problems such as diabetes or CHF that were entered or linked during a visit, which is the common workflow. Calling without a category returns the same problem repeated once per linked encounter, including inactive ones. Both outcomes produce a confidently wrong problem summary.
- **Recommendation:** In the agent's condition tool, query without a category (or both categories), dedupe by the underlying lists record (match code or title plus onset date, since FHIR ids differ), and compute active status client-side. Add a fixture test that seeds a medical_problem linked to an encounter and asserts it appears exactly once in the normalized problem list.

#### ARCH-4

**Encounter sensitivity ACL is enforced in the UI and update path only; FHIR search returns sensitive encounters** — *medium*

- **Evidence:** interface/patient_file/encounter/forms.php:563 gates encounter form rendering on aclCheckCore('sensitivities'). interface/patient_file/history/encounters.php:506-508 hides the rows in the encounter list. EncounterService.php:449-451 is inside updateEncounter (starts at line 429). EncounterService::search (152) selects fe.sensitivity (192) with no ACL filter. A grep for 'sensitiv' in src/Services/FHIR finds no enforcement, and FhirEncounterService uses EncounterService (line 79). Default ACLs in library/classes/Installer.class.php: Physicians get 'sensitivities' => ['normal','high'] (1202), Administrators get both (1135), Clinicians get only ['normal'] (1235).
- **Impact on the Co-Pilot:** Encounters marked sensitive (typically behavioral health or substance use) that the physician's UI hides can reach the agent through FHIR Encounter, and possibly through linked notes and diagnoses. The agent would then show a clinician PHI they are not authorized to see in OpenEMR, which is both a compliance incident and an authorization inconsistency.
- **Recommendation:** Before the Co-Pilot launches, decide the policy with the clinic, e.g. exclude sensitive encounters entirely. Because the FHIR Encounter resource does not expose `sensitivity`, you have three options: patch EncounterService::search and the related note/diagnosis services to apply aclCheckCore('sensitivities', ...) (needs a derived image); use a narrow read of the sensitivity flag; or leave encounter-linked data out of the MVP and document the limitation.
- **Verifier correction:** The bypass is real, but the stated impact on the target user is overstated. OpenEMR's default gacl gives the Physicians group access to both 'normal' and 'high' sensitivity, so a default-configured PCP already sees these encounters in the UI and FHIR adds no new exposure for them. The gap matters only for Clinicians-group users (nurses/MAs, normal only) or for sites with customized sensitivity ACLs. Lowered to medium; decide the policy with the clinic, but it does not block a physician-only MVP.

#### ARCH-5

**EHR launch: patient comes from session pid, app runs in an iframe modal, and a login screen appears unless the client is confidential, admin-enabled, and has both skip flags set** — *medium*

- **Evidence:** library/js/utility.js:575-601 (not library/utility.js) builds ehr-launch-client.php?client_id&csrf_token&intent and calls dlgopen(..., {allowExternal: true}). library/dialog.js:630 renders the iframe#modalframe. SmartLaunchController.php:125-135 reads pid and encounter from the session. SMARTLaunchToken.php:107-135 serialize has only {e,p,i,apt}, encrypted with encryptStandard (drive key), with no expiry or user binding. AuthorizationController.php:309-329 allows user/ scopes only for application_type 'private'. ScopeRepository.php:341-360 requires manual approval for confidential clients with user/system scopes and for public clients with launch. sql/database.sql:14126-14127 is_enabled DEFAULT '0' and skip_ehr_launch_authorization_flow DEFAULT '0'. AuthorizationController.php:593-597 skips only when a launch is present, shouldSkipAuthorizationFlow is true and a logged-in core user UUID is resolvable. shouldSkipAuthorizationFlow is at 1653-1672. library/globals.inc.php:3297-3302 oauth_ehr_launch_authorization_flow_skip defaults to '1'.
- **Impact on the Co-Pilot:** Out of the box, every launch shows OpenEMR's OAuth login and consent screens inside a modal. That alone eats the 90-second window. The launch patient is whatever pid is in the shared PHP session at click time. (Inference) With two browser tabs or windows on one OpenEMR session, it can differ from the chart the physician is looking at, which is a wrong-patient risk. The app lives in a cross-site iframe, so its own cookies are third-party. (Inference) Safari and Chrome may block them.
- **Recommendation:** Register the Co-Pilot as application_type 'private' with client_secret_post. Point initiate_login_uri and redirect_uri at the agent backend and do the token exchange server-side. An admin must enable the client and enable 'skip authorization flow' (global default is already 1). Show a patient banner (name, DOB, MRN read from the token-context Patient) at the top of every answer. Keep app state server-side or in memory rather than relying on third-party cookies. Record the launch-to-first-answer time as an SLO.
- **Verifier correction:** The file path is wrong: it is library/js/utility.js. The title's 'both skip flags' overstates the setup work, because the global skip flag already defaults to 1 and only the per-client flag (default 0) plus is_enabled need admin action. Skipping does not itself require a confidential client; confidentiality is needed only for user/ scopes. The wrong-patient risk from the session pid is inference: OpenEMR's main UI tracks one active patient per session, so the risk mostly comes from two browser windows. The third-party cookie concern is also inference. Once the client is configured this is launch friction plus an inferred risk, so lowered to medium.

#### ARCH-7

**Per-request API overhead, with no batching or pagination, drives briefing latency** — *medium*

- **Evidence:** BearerTokenAuthorizationStrategy.php:161 revoked check (AccessTokenRepository.php:106-110), 168-169 trusted user (TrustedUserService.php:21-25), 222 setupSessionForUserRole with 250-251 auth group, 292 expiry (AccessTokenRepository.php:56), 316 'API success' audit insert. SiteSetupListener.php:202 requires interface/globals.php. src/RestControllers/FHIR/Finder/FhirRouteFinder.php:25 includes the 876-line route file and 30-32 dispatch RestApiCreateEvent. HttpRestRouteHandler.php:58-60 matches routes linearly. ApiResponseLoggerListener.php:52-104 logs the full body when api_log_option=2 (the default, globals.inc.php:2893-2901). FhirObservationService.php:64-73 constructs 10 mapped services. UuidRegistry.php:411-437 runs a count(*) per table plus UPDATEs.
- **Impact on the Co-Pilot:** A one-patient briefing needs about 6-9 FHIR calls. Each carries roughly 10 bootstrap and auth queries, an audit insert, and a full-body log write before and after the clinical SQL, and results are unbounded for long-history patients. Calls made one after another will blow the 90-second budget; even in parallel, the slowest query (meds UNION, Observation fan-out) sets the latency.
- **Recommendation:** Run all FHIR reads in parallel (asyncio.gather) with category filters (vital-signs, laboratory) and date lower bounds. Start fetching the moment the launch completes, before the physician asks anything. Cache normalized results per (token, patient) for the session. Measure p50/p95 per resource on Railway with a realistic seeded patient before choosing an LLM round-trip strategy. Consider api_log_option=1 only after the compliance review (see ARCH-11).
- **Verifier correction:** Evidence is accurate apart from small line drift. The finding misses the largest per-query write amplifier, audit_events_query=1, which writes a log row for every patient-table SELECT (see missed findings).

#### ARCH-8

**SMART-critical globals are off or empty by default, and the Docker env-to-globals sync is fragile** — *medium*

- **Evidence:** library/globals.inc.php:3250-3255 rest_fhir_api default '0' and 3243-3248 site_addr_oath default ''. src/FHIR/Config/ServerConfig.php:44 uses `?? $_SERVER['HTTP_HOST']`, but because '' is not null the real fallback is interface/globals.php:646-647. When site_addr_oath is empty it is set from $ResolveServerHost (interface/globals.php:204-227), which uses REQUEST_SCHEME (defaulting to https only if unset) plus X-Forwarded-Host/HTTP_HOST and ignores X-Forwarded-Proto. CustomAuthCodeGrant.php:98-103 rejects a launch when aud is not in expectedAudience. docker/release/utilities/devtoolsLibrary.source:171 runs `mariadb --skip-ssl ... UPDATE globals`. openemr.sh:802 and 823 call `setGlobalSettings || true`. Bundle fullUrl uses site_addr_oath (FhirAllergyIntoleranceRestController.php:218, FhirGenericRestController.php:133).
- **Impact on the Co-Pilot:** Behind Railway's TLS-terminating proxy, a missing or wrong site_addr_oath yields wrong iss/aud and token/authorize URLs in .well-known/smart-configuration, and wrong Bundle fullUrls. The launch then fails with 'Aud parameter did not match', or the app follows http:// links. (Inference) OPENEMR_SETTING_* env vars may fail silently against MySQL 9.4 because of --skip-ssl, and if they do apply they overwrite admin-UI changes on every restart.
- **Recommendation:** Set rest_fhir_api=1, site_addr_oath=https://<public Railway domain>, and oauth_ehr_launch_authorization_flow_skip=1, then verify them from outside: /apis/default/fhir/.well-known/smart-configuration must show https issuer, authorize and token URLs on the public domain. Pick one source of truth, either env vars (after confirming setGlobalSettings actually runs against MySQL 9.4) or the admin UI, and document which.
- **Verifier correction:** The ServerConfig.php:44 `?? HTTP_HOST` fallback cited as evidence never fires, because the global defaults to '' (not null) and interface/globals.php:646-647 populates it first. The actual risk is sharper than stated: behind Railway's TLS-terminating proxy, if Apache receives plain HTTP, REQUEST_SCHEME='http' produces http://<public-host> for iss, aud, token URLs and fullUrls. That mismatches the https aud the app sends, so the launch fails with 'Aud parameter did not match'. The --skip-ssl failure against MySQL 9.4 remains inference.

#### ARCH-9

**Deployed image code is not this repo, and only sites/ persists, so any module or route change needs a derived image** — *medium*

- **Evidence:** version.php:18-21 is 8.2.0-dev and line 34 $v_database = 541, while the deployment runs openemr/openemr:8.5.0. docker/production/docker-compose.yml:27 pins openemr/openemr:latest@sha256 and 32-34 persists only /var/log and sites. openemr.sh:865-879 locks sqlconf.php to 400 and sites/default to 500. src/Core/ModulesApplication.php:141-155 loads active modules from the custom module path and, if a module's bootstrap is missing, runs `UPDATE modules SET mod_active = 0` (line ~155).
- **Impact on the Co-Pilot:** Code-level fixes in this fork (e.g., the sensitivity filter from ARCH-4, a custom Co-Pilot dashboard card module, a RestApiCreateEvent briefing endpoint) do nothing on Railway unless a custom image is built. Behavior verified by reading this repo may also differ from 8.5.0 (inference), so the agent's tool contracts need to be checked against the live server.
- **Recommendation:** Keep the MVP integration SMART-only with zero PHP changes, and treat OpenEMR as a black box verified through /fhir/metadata and live contract tests. If a PHP change becomes necessary, build `FROM openemr/openemr:8.5.0` with the patch or module copied in, pin the image digest, and run the same contract tests in CI.
- **Verifier correction:** Addition: a custom module installed through a derived image is automatically deactivated in the DB if a later deploy runs an image without that module's files (ModulesApplication.php ~155). This strengthens the case for pinning a derived image digest.

#### ARCH-11

**API audit trail records patient_id=0 for user-role FHIR reads and stores full PHI response bodies by default** — *medium*

- **Evidence:** ApiResponseLoggerListener.php:78 `$patientId = (int)($session->get('pid', 0));`. Session pid is set only for the patient role (BearerTokenAuthorizationStrategy.php:266-267); the user-role launch sets only the request patient UUID (populateTokenContextForRequest, ~441). ApiResponseLoggerListener.php:58-67 logs getContent() into both request_body and response when api_log_option=2 (default at globals.inc.php:2893-2901). api_log.response is longtext (database.sql:92-105). Local API calls are excluded (line 53). EventAuditLogger.php:660-661 notes that the encryption path was removed, so the bodies are stored in plaintext (LogTablesSink.php:98 inserts into api_log).
- **Impact on the Co-Pilot:** OpenEMR's own audit log can't answer 'which patients did the Co-Pilot read for Dr. X today' without parsing URLs and bodies. At the same time it keeps a second full copy of every PHI bundle the agent fetches, which grows the DB and adds retention and breach surface. Langfuse traces would become a third copy.
- **Recommendation:** Have the agent backend write its own structured access-audit record (user fhirUser, patient UUID, resources/ids read, correlation id, timestamp) to a store you control. Keep PHI out of Langfuse inputs and outputs or redact it there. Make an explicit compliance decision on api_log_option (full vs minimal) and on api_log retention and purge.
- **Verifier correction:** Add that api_log PHI bodies are stored unencrypted: EventAuditLogger.php:660-661 notes the encryption path was removed.

#### ARCH-M3

**Default SQL SELECT auditing writes log and log_comment_encrypt rows for every patient-table query in each FHIR call, with patient id 0 for API users** — *medium*

- **Evidence:** library/globals.inc.php:2778-2783 enable_auditlog default '1' and 2832-2837 audit_events_query default '1' ('Enable logging of all SQL SELECT queries'). src/Common/Database/QueryUtils.php:217-222 notes that auditSQLEvent is embedded in ADODB Execute for non-NoLog queries (library/ADODB_mysqli_log.php). EventAuditLogger.php:405-526 logs SELECTs touching LOG_TABLES (107-127: lists, patient_data, form_encounter, form_vitals, issue_encounter, etc.) with bound values, and sets pid from session 'pid' (512-516), which is unset for user-role API calls. LogTablesSink.php:60 and 94 perform synchronous inserts into log and log_comment_encrypt per event.
- **Impact on the Co-Pilot:** Each FHIR search runs several logged SELECTs (the UNION meds query, Condition sub-services, UUID-registry reads). Each one adds two synchronous INSERTs before the response returns, compounding the per-request overhead in ARCH-7 across 6-9 parallel calls on a single Railway MySQL. The resulting audit rows record full query text with bound values but patient id 0, so they neither attribute access to a patient nor stay small.
- **Recommendation:** Measure p95 per FHIR resource with audit_events_query on and off on Railway. Leave it enabled unless compliance review approves otherwise, and weigh it against the agent-side structured access log recommended in ARCH-11. Include log and log_comment_encrypt growth in DB sizing and retention planning.

#### ARCH-10

**Service-layer change events miss major write paths (eRx, HL7 labs, CDA import), so OpenEMR events can't drive agent cache invalidation** — *low*

- **Evidence:** src/Services/Traits/ServiceEventTrait.php:16-21 dispatchSaveEvent is used by PatientIssuesService, VitalsService, EncounterService and SocialHistoryService. Raw inserts that bypass it: interface/eRxStore.php:451-453, oe-module-weno/src/Services/LogDataInsert.php:22, src/Services/Cda/CdaTemplateImportDispose.php:1301 (prescriptions) and 1707 (procedure_result), interface/orders/orders_results.php:214, interface/orders/receive_hl7_results.inc.php:142 (rhl7InsertRow).
- **Impact on the Co-Pilot:** A design that pre-computes briefings and invalidates them on OpenEMR events would serve stale meds and labs, e.g. missing a potassium result that arrived by HL7 import or a new e-prescription. For a between-rooms briefing, stale data can't be told apart from current data.
- **Recommendation:** Don't build a persistent briefing cache on OpenEMR events. Fetch on launch, cache only for the session (minutes), and show 'data as of <fetch time>' in the UI. If a warm cache is ever needed, poll with _lastUpdated where supported (AllergyIntolerance, Condition, Observation) rather than relying on events.
- **Verifier correction:** The evidence is accurate, but this warns against a design (an event-invalidated precomputed briefing cache) that the stated architecture does not plan: SMART launch with on-demand FHIR reads. Lowered to low, as a design constraint to record rather than a current risk.

#### ARCH-12

**FHIR ids (UUIDs) are backfilled lazily inside GET requests, so citation ids are not guaranteed stable across restores or imports** — *low*

- **Evidence:** sql/database.sql uuid binary(16) DEFAULT NULL at 2024 (form_encounter), 2420 (form_vitals), 7673 (lists), 8300 (openemr_postcalendar_events), 8336 (patient_data), 8700 (prescriptions), 10494 (procedure_result). Constructors call createMissingUuidsForTables: AllergyIntoleranceService.php:39, PrescriptionService.php:41, VitalsService.php:53, ObservationLabService.php:37. UuidRegistry.php:295-321 runs a transactional backfill. SmartLaunchController.php:68 backfills patient_data. eRx and HL7 inserts (eRxStore.php:451, orders_results.php:214) do not set uuid.
- **Impact on the Co-Pilot:** Records inserted by eRx, HL7, CDA or seed scripts have no FHIR id until some FHIR read triggers a write transaction, which adds latency to the first read after an import. If the DB is restored from a dump taken before the backfill, new random UUIDs are generated, so source_ids in saved briefings, eval fixtures or Langfuse traces no longer resolve.
- **Recommendation:** After seeding or importing test data, warm UUIDs once by calling every resource for every patient, then take the eval fixture snapshot. Store citations with FHIR id plus resource type plus a content hash (e.g., code + date + value) so a verifier can match them again after a restore.
- **Verifier correction:** The lazy backfill and first-read latency are real, but the stability claim is overstated. Once assigned, UUIDs are persisted in the row and the uuid_registry, so any restore or dump taken after the backfill keeps the same FHIR ids. Instability only affects records that had no UUID at dump time, a narrow case for eval fixtures that the auditor's own 'warm then snapshot' recommendation already fixes.

### Data Quality

| ID | Severity | Finding | Verification |
|---|---|---|---|
| [DQ-1](#dq-1) | critical | FHIR cannot distinguish 'No Known Allergies' from 'nothing recorded' (lists_touch not exposed) | Confirmed |
| [DQ-2](#dq-2) | critical | Uncoded allergies come back with code 'Unknown'; reaction comments, dates and category are lost | Confirmed |
| [DQ-3](#dq-3) | critical | MedicationRequest UNIONs prescriptions and the med list: duplicates and stale or contradictory status | Confirmed with corrections |
| [DQ-4](#dq-4) | high | MedicationRequest dosage and supply mapped wrong: dose built from strength, no text for numeric dose, refills always 0, no start/end dates | Confirmed |
| [DQ-5](#dq-5) | high | Condition problem list split by encounter linkage; one Condition per encounter; occurrence 'First' reported as resolved | Confirmed with corrections |
| [DQ-7](#dq-7) | high | Lab Observations: OpenEMR statuses collapse to 'unknown', ranges with 0 lower bound dropped, blank abnormal flag looks normal | Confirmed with corrections |
| [DQ-M1](#dq-m1) | high | Soft-deleted vitals forms and removed clinical notes are still served via FHIR as current | Found by verifier |
| [DQ-M3](#dq-m3) | high | CCDA/Synthea import guarantees duplicate MedicationRequests with the drug code misfiled as reasonCode | Found by verifier |
| [DQ-8](#dq-8) | medium | Vitals: each form yields a data-absent Observation for every unfilled column | Confirmed |
| [DQ-9](#dq-9) | medium | Visit notes largely invisible via FHIR: only form_clinical_notes mapped, SOAP notes not | Confirmed |
| [DQ-11](#dq-11) | medium | Temporal fidelity: naive local datetimes with current-offset stamping, UTC default, a UI minutes/month bug, missing review dates | Confirmed |
| [DQ-12](#dq-12) | medium | Repo demo data can't support the demo: 14 dirty demographics rows, no clinical data; realistic import bypasses audit | Confirmed |
| [DQ-M2](#dq-m2) | medium | Condition resources never carry ICD-10/SNOMED codes (diagnosis string never parsed) | Found by verifier |
| [DQ-10](#dq-10) | low | No patient-level dedup guarantee; FHIR Patient hides duplicates (always active, no link) | Confirmed with corrections |

#### DQ-1

**FHIR cannot distinguish 'No Known Allergies' from 'nothing recorded' (lists_touch not exposed)** — *critical*

- **Evidence:** sql/database.sql:7744-7749 (lists_touch pid,type,date); library/lists.inc.php:18-20 (comment), :130-139 getListTouch, :141-151 setListTouch (inserts once, returns early if present, never updates date); templates/patient/card/allergies.html.twig:27-35 ('No Known Allergies' if listTouched, else 'Nothing Recorded'); interface/patient_file/summary/demographics.php:1128 passes listTouched to that card; grep of src/ for lists_touch hits only src/Common/Logging/EventAuditLogger.php:759; src/Services/AllergyIntoleranceService.php:101 filters type='allergy' only; hard deletes at AllergyIntoleranceService.php:361 (REST) and interface/patient_file/deleter.php:291 (UI issue delete) leave the lists_touch row in place.
- **Impact on the Co-Pilot:** An empty AllergyIntolerance Bundle comes back both for a patient with documented NKA and for one never asked. A briefing that says 'No allergies' or 'NKDA' is unsafe (for example before prescribing antibiotics), and a deleted allergy list still reads as NKA in the UI. No 'last reviewed' date exists to judge staleness.
- **Recommendation:** Treat an empty allergy bundle as 'No allergy entries recorded; cannot confirm NKA'. Add an eval case for this. Optionally add a read-only custom FHIR extension or operation that exposes lists_touch (with date) for allergy, medication and medical_problem, scoped by the same SMART patient/user access control.
- **Verifier correction:** The evidence is accurate. Two additions. (1) OpenEMR's own 'No Known Allergies' signal is weaker than the finding suggests. setListTouch runs on ANY save of an issue of that type (interface/patient_file/summary/add_edit_issue.php:279, interface/eRxStore.php:623, interface/forms/fee_sheet/review/fee_sheet_queries.php:125), not only on the explicit 'None' checkbox (stats_full.php:488 posting to library/ajax/lists_touch.php). Exposing lists_touch as an NKA extension would therefore carry a weak signal and should be labelled 'allergy list touched on DATE', not 'NKA'. (2) The most common deletion path is the UI deleter.php:291, not the REST delete. A related trap: clinicians often type 'NKDA' as an allergy title. That entry is uncoded, so it comes back as an AllergyIntolerance with code 'Unknown' (see DQ-2), and the current agent/fhir.py:71 would report an allergy to 'Unknown'.

#### DQ-2

**Uncoded allergies come back with code 'Unknown'; reaction comments, dates and category are lost** — *critical*

- **Evidence:** src/Services/FHIR/FhirAllergyIntoleranceService.php:198-218 (code only from lists.diagnosis, else UtilsService::createDataAbsentUnknownCodeableConcept at UtilsService.php:326-335 with display 'Unknown'); :221 title only in the text narrative; :193-196 builds a DAR reaction but never adds it; :135 category hard-coded 'medication'; :138-151 maps 'moderate' to criticality 'low'; :223-237 verification defaults to 'unconfirmed'; no setOnset/setRecordedDate/addNote in the file even though lists has begdate and comments (sql/database.sql:7680,7689); AllergyIntoleranceService.php:124-129 selects comments but they are never mapped; sql/database.sql:5480-5483 reaction list (unassigned, hives, nausea, shortness_of_breath); :6874 entered-in-error verification option with no filtering in AllergyIntoleranceService::search; agent/fhir.py:40-44,71 _text() returns coding display 'Unknown'.
- **Impact on the Co-Pilot:** A free-text 'Penicillin' allergy shows as 'Allergy: Unknown (unconfirmed)'. Anaphylaxis written in comments is invisible, and a missing reaction looks like no reaction. Latex and food allergies are labelled drug allergies. Entered-in-error allergies look current. The agent cannot say when an allergy was recorded.
- **Recommendation:** In the tool layer, build the label as code.text, then non-DAR coding.display, then text.div (XHTML stripped), then code. Treat data-absent-reason codings as missing. Filter out verificationStatus entered-in-error/refuted. Show 'reaction not recorded' explicitly. Do not trust category. Pair with a non-FHIR or custom read of lists.comments, or flag 'see chart comments'. Seed demo data with an uncoded allergy whose reaction is only in comments.
- **Verifier correction:** Confirmed, with one strengthening point. The default allergy quick-pick list (sql/database.sql:5192-5196: penicillin, sulfa, iodine, codeine) has no codes, so the standard UI entry path produces uncoded allergies, and the 'Unknown' label is the common case, not an edge case. Minor extra defects: FhirAllergyIntoleranceService.php:184 reads undefined $display, so reaction display is always reaction_title. A reaction option without codes stays a string (AllergyIntoleranceService.php:127) and is iterated with foreach at FHIR :175.

#### DQ-3

**MedicationRequest UNIONs prescriptions and the med list: duplicates and stale or contradictory status** — *critical*

- **Evidence:** src/Services/PrescriptionService.php:149-261 UNION; :258-260 excludes list rows whose lists_medication.prescription_id IS NOT NULL; status CASE :180-184 (prescriptions) and :234-238 (lists), with no date comparison; :212 intent 'plan'; :217 NULL rxnorm_drugcode; templates/prescription/general_edit.html.twig:154-155 has only a 'Currently Active' checkbox and no end_date input; src/Services/PatientIssuesService.php:145-156 ending a med-list issue only sets prescriptions.medication=0, never active=0; library/classes/Prescription.class.php:316-333 links by prescription_id or upper(trim(title)); gen_lists_medication :648-673 sets prescription_id. CCDA import: src/Services/Cda/CdaTemplateImportDispose.php:1300-1357 inserts prescriptions with medication=0, and :1398-1413 inserts a separate lists 'medication' row with diagnosis=drug_code and no lists_medication link. src/Services/FHIR/FhirMedicationRequestService.php:241-251 turns diagnosis into reasonCode with a default system of SNOMED.
- **Impact on the Co-Pilot:** The same drug appears twice with different statuses. A med discontinued on the med list still reads 'active' from its linked prescription. Any prescription never unchecked stays active for years, and a future end date shows 'completed' today. Filtering on intent=order drops the reconciled med list, and reasonCode may be the drug itself. For a 90-second med review this means false current meds (for example anticoagulants).
- **Recommendation:** Tool layer: normalise drug name / RxNorm and group both sources, show both entries when statuses differ, and label entries older than a configurable age as 'possibly stale (authored YYYY-MM)'. Never filter by intent, and ignore reasonCode for list-sourced meds. Seed demo data with linked, unlinked, title-mismatch and stale-active cases.
- **Verifier correction:** The duplicate claim is overstated for the UI path. The UNION deliberately excludes med-list rows linked through lists_medication.prescription_id, so a UI prescription added to the med list does NOT appear twice. Duplicates arise from unlinked rows: manual med-list entries plus prescriptions with 'medication' unchecked, or a title mismatch. On the CCDA/Synthea import path, however, every imported med appears twice: CdaTemplateImportDispose.php inserts both an unlinked prescriptions row and a lists row. The reasonCode is then the RxNorm drug code labelled as SNOMED. The staleness claims are confirmed: discontinuing via the Issues screen leaves the linked prescription active=1 (PatientIssuesService.php:149-155 updates only 'medication'), UI prescriptions have no end date, and any end_date with active=1 reads 'completed' regardless of the date.

#### DQ-4

**MedicationRequest dosage and supply mapped wrong: dose built from strength, no text for numeric dose, refills always 0, no start/end dates** — *high*

- **Evidence:** templates/prescription/general_edit.html.twig:212-227 'Medicine Units' = size + unit (strength); :239-240 structured dosage input maxlength 10 (simplified mode :233-234 allows a 100-char free-text SIG); src/Services/FHIR/FhirMedicationRequestService.php:264-268 text only if non-numeric; :291-296 doseQuantity = intval(prescription_drug_size) with unit_title; :316-329 numberOfRepeatsAllowed = refills ?? 0 and quantity labelled with unit_title; src/Services/PrescriptionService.php:95-148 outer SELECT has no refills/start_date/end_date/indication although the prescriptions table has them (sql/database.sql:8708,8720,8734,8735); :297 and :306 both join list_id 'medication_adherence' (the information-source join uses the wrong list); agent/fhir.py:87 reads only dosageInstruction[0].text.
- **Impact on the Co-Pilot:** Structured UI prescriptions come back with no dosage text, so the agent says 'dose not recorded'. If it reads doseAndRate it reports strength as dose (500 mg for 2 tabs), and 0.5 truncates to 0. Dispense quantity shows as '30 mg'. Refills always read 0. The agent cannot say when a med started or ended.
- **Recommendation:** Build the sig from doseAndRate + timing.code.text + route.text + text. Label doseQuantity as 'strength' unless drug_dosage_instructions exists. Never state refill counts or duration from FHIR. Show authoredOn as 'ordered on' only. Document these as known limits in AUDIT.md.
- **Verifier correction:** Confirmed as stated. One nuance: prescriptions.drug_dosage_instructions defaults to NULL, so line 264 falls back to 'dosage'. In SIMPLIFIED_PRESCRIPTIONS mode that field holds a full free-text SIG, so dosage text IS present. Loss of dosage text applies to the default structured form, where dosage is numeric.

#### DQ-5

**Condition problem list split by encounter linkage; one Condition per encounter; occurrence 'First' reported as resolved** — *high*

- **Evidence:** src/Services/FHIR/Condition/FhirConditionProblemListItemService.php:122-126 (type medical_problem, activity=1, issue_encounter list_id :missing) with LEFT JOIN issue_encounter :175-180; FhirConditionEncounterDiagnosisService.php:163-192 INNER JOIN issue_encounter + form_encounter (one Condition per link); :53 UUID_CUTOVER_DATE and :239-253 reuse lists_uuid when issue_encounter.created_at (or begdate) predates the cutover; sql/database.sql:3446 created_at DEFAULT CURRENT_TIMESTAMP; Trait/FhirConditionTrait.php:110 occurrence==1 means 'resolved' while sql/database.sql:4393 occurrence '1'='First'; FhirConditionProblemListItemService.php:211-217 default 'unconfirmed' vs Trait:248-255 default 'confirmed' (encounter diagnoses); Trait:200-207 uses 'date', which the problem-list SQL aliases to condition_date (:153), so recordedDate falls back to begdate. Encounter linking happens at interface/forms/fee_sheet/review/fee_sheet_queries.php:99 and :113, and add_edit_issue.php:289-292.
- **Impact on the Co-Pilot:** Querying `category=problem-list-item` misses chronic problems addressed at any visit. An unfiltered query returns the same diabetes N times, possibly with duplicate resource ids that break citation mapping. Newly diagnosed 'first occurrence' problems appear resolved, and the same problem can be both confirmed and unconfirmed.
- **Recommendation:** Query Condition without category. Dedupe by resource id, then group by code/text and keep the most recent. Treat 'resolved' without abatementDateTime as unreliable and show it as 'status uncertain'. Key citations on (resourceType, id, encounter) rather than id alone.
- **Verifier correction:** fee_sheet_queries.php:125 is setListTouch, not the encounter link; linking is at :99 and :113. The call at :113 passes ($pid, $list_id, $encounter) against the signature (pid, encounter, issueId) at PatientIssuesService.php:273, so problems created from the fee sheet are mislinked. The duplicate-id claim does not apply to this project's fresh 8.5.0 deployment. issue_encounter.created_at defaults to CURRENT_TIMESTAMP (after the 2025-11-15 cutover), so issue_encounter.uuid is used. Duplicates only occur for upgraded historical rows or backdated imports with NULL created_at and an old begdate. The remaining claims (split by linkage, N Conditions per linked problem, First occurrence shown as resolved, inconsistent verification defaults) are confirmed.

#### DQ-7

**Lab Observations: OpenEMR statuses collapse to 'unknown', ranges with 0 lower bound dropped, blank abnormal flag looks normal** — *high*

- **Evidence:** sql/database.sql:10497-10509 procedure_result free-text units/range/abnormal/result_status, DEFAULT ''; :4577-4582 statuses final/prelim/cancel/error/correct/incomplete; interface/orders/receive_hl7_results.inc.php:262-281 HL7 F/P/C/X mapped to final/prelim/correct/error; src/Services/FHIR/Observation/FhirObservationLaboratoryService.php:357-364 passes only FHIR codes, otherwise 'unknown'; :266-268 array_filter drops 0.0 and keeps keys, so '0-5' gives no range and '<200' gives none; :251-261 LOINC system assumed, null-flavor code unless both result_code and result_text are set; :327 interpretation only when abnormal is non-empty; src/Services/ProcedureService.php:76-320 search() has no ORDER BY (the ORDER BY at :675 belongs to a different method); FhirServiceBase.php:262-268 plus :313-316 default WithConfig ignores the config, and only FhirPatientService overrides it (:924).
- **Impact on the Co-Pilot:** Corrected or erroneous results show status 'unknown' instead of entered-in-error, preliminary looks like final, and the agent may cite a wrong value. Many common ranges (0-X) are missing, so 'is this abnormal?' can't be answered. A blank abnormal flag gets read as normal. Unsorted results make 'latest A1c' unreliable.
- **Recommendation:** Tool layer: sort by effectiveDateTime; show 'status unknown' and exclude from trend statements; never infer normal from a missing interpretation; include raw value + unit + 'range not provided'. Seed labs with prelim/corrected/error statuses and 0-lower-bound ranges for evals.
- **Verifier correction:** 'Preliminary looks like final' is wrong: 'prelim' maps to 'unknown', the same as 'correct' and 'error'. The effect is that final is distinguishable but every non-final status collapses to 'unknown'. The cited :192-194 range_low/range_high bug writes fields that parseOpenEMRRecord never reads (it re-parses the range at :266-268), so it has no output impact. 'Blank abnormal flag looks normal' describes agent-side interpretation, not a server defect. The unsorted and no-paging claims are confirmed.

#### DQ-M1

**Soft-deleted vitals forms and removed clinical notes are still served via FHIR as current** — *high*

- **Evidence:** The UI deletes encounter forms by soft delete only: interface/patient_file/encounter/delete_form.php:57 `update forms set deleted=1`. src/Services/VitalsService.php:85-199 search(), which FhirObservationVitalsService.php:441 calls, selects forms.deleted but never filters on it. The UI helpers do filter: VitalsService.php:448, :464 and :509 add `deleted = 0`. src/Services/ClinicalNotesService.php:62-161 FHIR search has no forms.deleted or activity filter. Removing a note row sets activity=0 (interface/forms/clinical_notes/save.php:73-79). src/Services/FHIR/DocumentReference/FhirClinicalNotesService.php:166-174 maps it with `if (!empty($dataRecord['activity']))`, so activity 0 falls to the else branch and gets status 'current' instead of 'entered-in-error'.
- **Impact on the Co-Pilot:** Vitals entered on the wrong patient or encounter and then deleted, and notes a clinician removed, still reach the agent as valid, current data. The agent may cite a retracted BP or weight, or a deleted note, as the latest finding, and nothing in the resource flags it.
- **Recommendation:** State this in AUDIT.md as a server-side defect. For the MVP, exclude DocumentReference entries whose meta or date conflicts cannot be verified, or cross-check through a read-only backend query. Preferably patch the fork: add a forms.deleted=0 filter to VitalsService::search and ClinicalNotesService::search, and map activity=0 to entered-in-error. Add a seeded deleted vitals form and a removed note to the eval set.

#### DQ-M3

**CCDA/Synthea import guarantees duplicate MedicationRequests with the drug code misfiled as reasonCode** — *high*

- **Evidence:** src/Services/Cda/CdaTemplateImportDispose.php:1300-1357 inserts a prescriptions row (medication=0, with end_date). :1398-1413 then inserts a separate lists row (type 'medication', diagnosis=drug_code) with no lists_medication row, so prescription_id IS NULL and the row passes the UNION filter at src/Services/PrescriptionService.php:258-260. The import is reached via interface/modules/zend_modules/module/Carecoordination/src/Carecoordination/Model/CarecoordinationTable.php:952,1953 calling InsertPrescriptions. src/Services/FHIR/FhirMedicationRequestService.php:246-248 turns the lists diagnosis (the RxNorm drug code) into reasonCode with a default SNOMED system. The UPDATE branch at CdaTemplateImportDispose.php:1358-1396 binds parameters in the wrong order (extension goes into medication, pid into request_intent), so re-imports corrupt rows [inference from positional binds].
- **Impact on the Co-Pilot:** If demo data comes from the Synthea/CCDA devtool, every med appears twice: once as intent=order (possibly 'completed') and once as intent=plan (active or stopped). The agent will list duplicate or contradictory meds and may state the drug as its own indication.
- **Recommendation:** Dedupe meds in the tool layer by normalized name and RxNorm, preferring the prescriptions source. Ignore reasonCode for list-sourced rows. If CCDA import is used for seed data, run the evals against it explicitly, or seed meds through the UI prescription flow with 'Add to Medication List' checked so rows are linked.

#### DQ-8

**Vitals: each form yields a data-absent Observation for every unfilled column** — *medium*

- **Evidence:** sql/database.sql:2427-2446 form_vitals numeric columns DEFAULT '0.00', bps/bpd varchar(40); src/Services/FHIR/Observation/FhirObservationVitalsService.php:433-437 defaults to all COLUMN_MAPPINGS plus calculated codes when no code param is sent; :560-596 emits one record per mapped code regardless of value; :818-825 sets dataAbsentReason when there is no quantity; :869-872 value only if floatval > 0; :879-880 BP DAR when both are 0; src/Services/VitalsService.php:254-270 units lb/in/degF or kg/cm/Cel per global.
- **Impact on the Co-Pilot:** 'Latest weight' or 'latest BP' picks the newest Observation, which is often a DAR placeholder from a visit where only BP was taken, so the agent reports 'unknown' although a real value exists one visit back. Vitals bundles are roughly 12x larger than the data, which adds latency and tokens.
- **Recommendation:** Filter Observations with dataAbsentReason (and components with DAR) before selecting the latest per LOINC code. Request specific codes (`code=8480-6,...`) and date bounds to cut bundle size. Always show the value's date.
- **Verifier correction:** Evidence confirmed. '~12x larger' is an inference: there are about 19 mapped codes plus calculated codes per form, so bloat can be higher. The code= filter at FhirObservationVitalsService.php:413-431 does work, so the recommendation to request specific codes is valid.

#### DQ-9

**Visit notes largely invisible via FHIR: only form_clinical_notes mapped, SOAP notes not** — *medium*

- **Evidence:** src/Services/ClinicalNotesService.php:25 TABLE_NAME form_clinical_notes; src/Services/FHIR/DocumentReference/FhirClinicalNotesService.php:122-127 base64 text/plain of description, and :208-213 only notes without clinical_notes_category (categorized notes go to FhirDiagnosticReportClinicalNotesService); src/Services/EncounterService.php:614-620 getSoapNotes (form_soap), used only outside FHIR; grep -rl form_soap src/Services/FHIR returns nothing.
- **Impact on the Co-Pilot:** For practices documenting in the SOAP form, the agent cannot summarize the last visit's assessment and plan and will say 'no recent notes'. That undermines the main 'what happened last time' need in a 90-second pre-visit briefing.
- **Recommendation:** Scope the MVP to the resources that exist (problems, meds, allergies, labs, vitals, encounters). In the demo, write notes with the Clinical Notes form. State the gap in AUDIT.md, and if needed add a read-only custom DocumentReference mapping for form_soap as a later capability.
- **Verifier correction:** Confirmed. Partial mitigation not mentioned: the encounter reason/chief complaint is exposed as Encounter.reasonCode.text (src/Services/FHIR/FhirEncounterService.php:228-236). Note also that removed or deleted clinical notes are still served as 'current' (see missed findings).

#### DQ-11

**Temporal fidelity: naive local datetimes with current-offset stamping, UTC default, a UI minutes/month bug, missing review dates** — *medium*

- **Evidence:** src/Services/FHIR/UtilsService.php:404-416 getLocalDateAsUTC uses new DateTimeZone(date('P')) (current offset), so historical DST dates are off by 1h and the output is not actually UTC; library/globals.inc.php:777-782 gbl_time_zone default '' ('If unassigned will default to php.ini'); interface/globals.php:509-518 date_default_timezone_set only if set, then MySQL SET time_zone to a fixed current offset; docker/development-easy-redis/php.ini:924 ';date.timezone =' [inference for the official image]; interface/patient_file/summary/add_edit_issue.php:270 date("Y-m-d H:m:s") (m = month) sets lists.date, which is MedicationRequest authoredOn for list-sourced meds (PrescriptionService.php:218); FhirAllergyIntoleranceService.php has no onset/recordedDate; library/lists.inc.php:141-151 never refreshes the touch date.
- **Impact on the Co-Pilot:** Timestamps can be off by 1 hour (DST) or by the full UTC offset, which can flip 'today' vs 'yesterday' for same-day vitals and labs. The minutes of Issue creation times are wrong. The agent cannot say how old an allergy entry is or when lists were last reconciled, so it may present a years-old list as current.
- **Recommendation:** Set gbl_time_zone (for example America/New_York) in the Railway deployment before entering demo data. The agent should compare and show dates at day granularity in clinic local time, show 'recorded YYYY-MM-DD' next to each fact where available, and say 'date not recorded' for allergies.

#### DQ-12

**Repo demo data can't support the demo: 14 dirty demographics rows, no clinical data; realistic import bypasses audit** — *medium*

- **Evidence:** sql/example_patient_data.sql: 14 INSERT INTO patient_data rows, no lists/prescriptions/procedure_result/form_vitals; 'Mrs.' + Male (Eduardo Perez), 'Mr.' + Female (Ilias Jenane), state 'California' (Wallace Buckley), phone '(5555) 555-1111', duplicate SSN 555-11-1111; CONTRIBUTING.md:576 Synthea random patients; contrib/util/ccda_import/import_ccda.php:12-15 development mode bypasses audit_master/audit_details and turns off the audit log, :43-44 requires OPENEMR_ENABLE_CCDA_IMPORT; src/Services/Cda/CdaTemplateImportDispose.php:1300-1357 prescriptions inserted with end_date and medication=0, :1398-1413 unlinked lists medication rows.
- **Impact on the Co-Pilot:** There is no clinical data to demo or evaluate against. Synthea CCDA import produces clean, coded data that hides the real failure modes above (DQ-1 to DQ-8), while at the same time triggering DQ-3 and DQ-6 (completed statuses, unnamed SNOMED problems). Development-mode import skips audit logging, which is a compliance-narrative caveat.
- **Recommendation:** Build a small hand-curated synthetic seed (5-8 patients) through the OpenEMR UI or API covering: NKA vs nothing recorded, uncoded allergy with comment-only reaction, linked and unlinked duplicate meds, stale active Rx, problem linked to several encounters, first-occurrence problem, lab with 0-lower-bound range and corrected status, partial vitals, duplicate patient. Optionally add Synthea CCDA for volume. Write evals against the seeded ground truth.
- **Verifier correction:** Line numbers corrected: import_ccda.php dev-mode note is at :12-15 and the env gate at :43-44, not :9-12. Additional strengthening point: CCDA import guarantees duplicate MedicationRequests (a prescriptions row plus an unlinked lists row per med), so Synthea demo data will exhibit DQ-3 on every medication.

#### DQ-M2

**Condition resources never carry ICD-10/SNOMED codes (diagnosis string never parsed)** — *medium*

- **Evidence:** src/Services/FHIR/Condition/Trait/FhirConditionTrait.php:138 emits codings only if is_array($dataRecord['diagnosis']). FhirConditionProblemListItemService.php:129-201 and FhirConditionEncounterDiagnosisService.php:115-227 return lists.diagnosis as a raw 'ICD10:E11.9' string and never call BaseService::addCoding (src/Services/BaseService.php:551-573), so Trait:152-156 always sets only code.text=title. The tests mask this by passing arrays directly (tests/Tests/Services/FHIR/Condition/FhirConditionService3_1_1Test.php:374,438). The 'code' search parameter maps to the raw diagnosis column (FhirConditionProblemListItemService.php:98).
- **Impact on the Co-Pilot:** Problem labels are whatever free text the clinician typed (abbreviations, misspellings), with no code to normalize, dedupe (DQ-5 grouping) or drive rule-based logic such as 'diabetic, so A1c due'. Filtering Conditions by code will silently return nothing. The LLM may add codes from memory, which cannot be cited.
- **Recommendation:** Group and dedupe Conditions on normalized text, not code. Never present ICD/SNOMED codes for Conditions unless they are returned. Optionally patch the fork to call addCoding() on diagnosis in both Condition services. Record in AUDIT.md that US Core Condition.code coding is missing in this build.

#### DQ-10

**No patient-level dedup guarantee; FHIR Patient hides duplicates (always active, no link)** — *low*

- **Evidence:** sql/database.sql:8467-8471 UNIQUE only on pid and uuid, non-unique name/DOB indexes; :8350 ss and :8379 pubpid not unique; :8452 dupscore DEFAULT -9; library/patient.inc.php:1151-1153 updateDupScore runs on UI patient creation but not on update (:1154-1157), and patient.inc.php:1675-1689 is the scorer; src/Services/FHIR/FhirPatientService.php:212 setActive(true) unconditionally, no setLink/addLink in the file; sql/example_patient_data.sql rows for John Dockerty/James Janssen share SSN '555-11-1111'.
- **Impact on the Co-Pilot:** The SMART launch binds to one pid, so labs or meds filed under a duplicate chart (HL7 import, registration error) are invisible. The agent confidently says 'no labs on file', with no signal that a probable duplicate exists.
- **Recommendation:** Phrase absence as 'none in this chart'. Optionally have the backend (not the LLM) check dupscore > threshold via a read-only endpoint and show a 'possible duplicate chart' banner. Include a duplicate patient in the seed data.
- **Verifier correction:** 'dupscore computed offline' is inaccurate. It is computed on UI patient creation (patient.inc.php:1153), but not on demographic edits or API/CCDA creation. The duplicate-chart risk is largely generic to EHRs, and the SMART launch binds the chart the physician opened. The codebase-specific parts (Patient always active, no Patient.link) are true, but the impact on a 90-second briefing is modest.

### Compliance & Regulatory

| ID | Severity | Finding | Verification |
|---|---|---|---|
| [COMP-3](#comp-3) | critical | BAA chain: agent code already sends PHI to Langfuse; Anthropic feature coverage, Railway BAA and DB transport encryption are unverified | Confirmed |
| [COMP-1](#comp-1) | high | Clinician FHIR/API access is logged without patient, client or outcome attribution | Confirmed with corrections |
| [COMP-2](#comp-2) | high | Audit tables are a plaintext full-PHI replica (api_log_option=2, SQL binds, deleted rows) and reading them is not audited | Confirmed with corrections |
| [COMP-4](#comp-4) | high | Minimum necessary is not technically enforced: user/ scopes, an always-true patient check, and a client-supplied patient_id | Confirmed with corrections |
| [COMP-6](#comp-6) | high | The agent's session cache is an unlogged, user-unbound PHI access path with no retention limit | Confirmed |
| [COMP-7](#comp-7) | high | No agent-side audit trail (who asked what about which patient) and no correlation id that reaches OpenEMR | Confirmed |
| [COMP-M1](#comp-m1) | high | patient/ scopes turn off OpenEMR's role-based ACL for clinician (users-role) tokens, and nothing checks user-to-patient access or ACL when scopes are granted | Found by verifier |
| [COMP-5](#comp-5) | medium | Encounter sensitivity restrictions are enforced in the UI but not on FHIR read/search paths | Confirmed with corrections |
| [COMP-8](#comp-8) | medium | Audit log tamper-evidence and retention are weak: unkeyed per-row hashes, an unaudited purge, a 1-2 year default window, ATNA off and fail-silent | Confirmed with corrections |
| [COMP-9](#comp-9) | medium | Breach investigation and notification scoping would depend on full-text scans and unreliable IP data | Confirmed with corrections |
| [COMP-M2](#comp-m2) | medium | The Co-Pilot is an ONC HTI-1 predictive DSI, and OpenEMR's client registration defaults dsi_type to 'none' with no source attributes | Found by verifier |
| [COMP-M3](#comp-m3) | medium | Every Co-Pilot FHIR call writes several unencrypted audit rows synchronously in-request, including a pre-auth HTTP-request row with a blank user and the patient uuid | Found by verifier |
| [COMP-10](#comp-10) | low | Accounting of disclosures is manual, and edits or deletions of it are not audited | Confirmed with corrections |
| [COMP-11](#comp-11) | low | No de-identification tooling, and optional OpenEMR telemetry can ship FHIR resource uuids to a non-BAA endpoint | Confirmed |
| [COMP-12](#comp-12) | low | Right of access is covered by the portal and FHIR, but agent outputs sit outside the designated record set, and the README overstates audit support | Confirmed |

#### COMP-3

**BAA chain: agent code already sends PHI to Langfuse; Anthropic feature coverage, Railway BAA and DB transport encryption are unverified** — *critical*

- **Evidence:** agent/fhir.py:57-62, :65-76, :79-91 use @observe(as_type="tool", capture_input=False) without capture_output. agent/main.py:140 does the same, and its return value (ChatResponse with answer and quoted chart values, :180) is captured. :150 sends the patient_id in metadata. In langfuse 3.7.0 (pinned in agent/requirements.txt), _client/observe.py:175-184 defaults capture_output to True unless LANGFUSE_OBSERVE_DECORATOR_IO_CAPTURE_ENABLED is false, and that variable is absent from agent/.env and agent/.env.example. agent/.env.example:7 and the local agent/.env both set LANGFUSE_HOST=https://us.cloud.langfuse.com, the standard US cloud region, which is not Langfuse's separate HIPAA region (external knowledge). agent/main.py:158-161 messages.parse(output_format=Briefing); agent/fhir.py:100 "strict": True. src/BC/DatabaseConnectionOptions.php:136-147 and DatabaseConnectionFactory.php:39-44 enable TLS only if documents/certificates/mysql-ca exists. SystemLogger.php:62-66 uses ErrorLogHandler, which goes to container stderr.
- **Impact on the Co-Pilot:** Langfuse, Anthropic and Railway each receive or store PHI, which makes each one a business associate. Without signed BAAs that cover the exact features and region in use, every real-patient query is an impermissible disclosure. Because the Railway MySQL cert has no hostname, a CA file would fail verification, so OpenEMR-to-MySQL traffic is most likely plaintext over Railway private networking (inference).
- **Recommendation:** Now: pass capture_output=False on all @observe decorators and send only counts, latency, resource ids and an HMAC pseudonym of the patient uuid to Langfuse. Alternatively, move to Langfuse's HIPAA offering under a BAA or self-host inside the boundary. Before real PHI: (1) confirm the Anthropic BAA/ZDR covers the org key, structured outputs and strict tool use; (2) sign a Railway BAA on the plan in use (verify availability), and document Railway private-network encryption as the 164.312(e) control, or configure MySQL TLS with a proper CA/hostname; (3) until all three are done, use Synthea data only and write that down in AUDIT.md.
- **Verifier correction:** Confirmed, and the evidence is stronger than stated: the configured Langfuse host is the standard US cloud endpoint, not a HIPAA region, so PHI in tool outputs and chat output currently goes to a non-BAA endpoint by default. Plaintext DB transport remains an inference. One minor nuance: the claude generation span sends only the stop_reason as output (main.py:162-163), not the message contents.

#### COMP-1

**Clinician FHIR/API access is logged without patient, client or outcome attribution** — *high*

- **Evidence:** src/RestControllers/Authorization/BearerTokenAuthorizationStrategy.php:247-265 (the users role sets authUser/authUserID/authProvider), :266-267 (session pid is set only for the patient role), :440-444 (the launch patient goes on the request object only). src/RestControllers/Subscriber/ApiResponseLoggerListener.php:75 (patient_id comes from session pid), :87-91 (success hard-coded to 1; getStatusCode never read). sql/database.sql:92-105 (api_log has only a PK and no client_id or status column). BearerTokenAuthorizationStrategy.php:316 (the token-use row has a blank user; the client id appears only in comments). EventAuditLogger.php:508-514 (SQL-audit pid from session), :368-371 (viewer filters on l.patient_id). AtnaSink.php:104-106. Mitigating: agent/fhir.py:59,67,81 always put the patient uuid in the URL (Patient/{id} and ?patient={id}), so request_url (a text column) carries it, and log.user/api_log.user_id do identify the clinician. Additional (inference from listener order): SiteSetupListener.php:132 loads interface/globals.php before authorization, and globals.php:848-849 calls logHttpRequest (EventAuditLogger.php:700-733, default on at globals.inc.php:2845-2850). That writes one more log row per API call, with a blank user and the rewritten query string (apis/.htaccess RewriteRule ... _REWRITE_COMMAND=$1 [QSA]), which includes the patient uuid.
- **Impact on the Co-Pilot:** Every Co-Pilot tool call (3+ per question x 20 patients/day) produces api_log rows with patient_id=0, success=1 and no client_id. A patient-centric audit or a patient complaint won't surface Co-Pilot access. Denied (403) and granted reads look identical. The only way to match a row to the Co-Pilot is by 1-second timestamps, and the agent sends its tool calls in parallel (agent/main.py:169). This fails the 164.312(b) audit-control intent and blocks breach scoping.
- **Recommendation:** For the MVP, don't patch core: have the agent write its own audit record that joins to api_log on user_id + exact request_url + timestamp (see COMP-7). For the fork, make one small change in ApiResponseLoggerListener: record `$request->getPatientUUIDString()` (resolved to pid) when session pid is empty, record `$response->getStatusCode()` and set success from it, and add client_id from `$request->getClientId()` (a column or the comments field). Add indexes on api_log(patient_id, created_time) and api_log(user_id, created_time). Test with one clinician EHR-launch token and confirm a non-zero patient_id and a real success flag.
- **Verifier correction:** Overstated. User attribution exists (log.user, api_log.user_id), and the patient uuid can be recovered from api_log.request_url for every call the agent makes. What's missing is a structured patient_id, client_id and real outcome, so the built-in patient filter misses these rows and denied calls look like successes. Patient-level reconstruction is possible but slow (LIKE on request_url), so this is high, not critical. There are also up to four loosely linked rows per call (the http-request row with a blank user and the uuid in the query string, the token-use row with the client and a blank user, SQL-audit rows with user but pid=0, and the api row with user but patient_id=0), and nothing joins them except the 1-second timestamp.

#### COMP-2

**Audit tables are a plaintext full-PHI replica (api_log_option=2, SQL binds, deleted rows) and reading them is not audited** — *high*

- **Evidence:** library/globals.inc.php:2893-2902 (api_log_option default '2'). ApiResponseLoggerListener.php:62-64, :83-85 (response body stored twice). Symfony JsonResponse and RestControllerHelper.php:78,81 send an exact 'application/json', which matches :118. LogTablesSink.php:89 'encrypt' => 'No'. EventAuditLogger.php:660-661 (encryption removed), :446-452 (bound values appended), :664 (base64 only). globals.inc.php:2832-2837 (audit_events_query=1). interface/patient_file/deleter.php:57-78 (full row copied into the delete event). interface/main/backup.php:1006-1009 (mkdir + chmod 0777), :1014-1026, gated by admin/super at :68. EventAuditLogger.php:498-505 (logview SELECT has no LOG_TABLES match, so it is dropped). BUT interface/logview/logview.php:29 plus :34-36 use GET, and interface/globals.php:849 with EventAuditLogger.php:700-733 (audit_events_http-request default 1, globals.inc.php:2845-2850) records every logview page hit as http-request-select, including the query string with the user and patient filters.
- **Impact on the Co-Pilot:** Every chart summary the agent pulls is saved again, in full, in api_log, and anyone with admin/users rights can read it without leaving a trace. The logs become the largest unencrypted PHI store, which enlarges the breach surface and conflicts with deletion requests. It also adds heavy synchronous insert load on each FHIR call: 2x response bodies plus 2 rows per audited SELECT (LogTablesSink.php:60,94), which lands on the agent's latency budget.
- **Recommendation:** Set api_log_option=1 (Minimal) on the Railway deployment, via OPENEMR_SETTING_api_log_option or the Globals UI. If a record of what data was released is needed, keep resource ids plus a sha256 of each response in the agent's own audit record instead of bodies. Keep audit_events_query on only if the extra latency is measured and acceptable, and document the choice. Restrict logview to a dedicated auditor ACL and audit access to it. Never use the backup.php event-log export on Railway without encrypting its output.
- **Verifier correction:** The claim that reading the logs leaves no trace is wrong: opening and filtering the Logs Viewer is recorded by the default-on HTTP request audit, with its GET filters. Only the result set, and direct DB reads, go unrecorded. 'Largest unencrypted PHI store' is also overstated: the primary clinical tables are just as unencrypted and sit in the same DB and trust boundary. The real harms are duplication, a wider access path through admin/users, and conflicts with retention and deletion. On latency: the SQL-audit rows are written synchronously inside the request, but the api_log insert runs on kernel.terminate, which may happen after the body is flushed (inference, depends on the SAPI). The Minimal-logging recommendation stands.

#### COMP-4

**Minimum necessary is not technically enforced: user/ scopes, an always-true patient check, and a client-supplied patient_id** — *high*

- **Evidence:** BearerTokenAuthorizationStrategy.php:479-485 always returns true, but it is only called for launch-context patient binding (:443), not for user/ scopes. Section-only ACL for user/ scopes: apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php:79 (AllergyIntolerance), :476 (MedicationRequest), :618 (Patient patients/demo), :169 (Condition via FhirGenericRestController). Patient binding: src/Common/Http/HttpRestRouteHandler.php:65-67, src/RestControllers/Subscriber/AuthorizationListener.php:143-150. Agent: agent/fhir.py:1-2 (user/ scopes by design), agent/main.py:107, :145-147, :127-128 (patient comes from the client body). FHIR_README.md:175-187 (granular scopes).
- **Impact on the Co-Pilot:** With user/*.rs, one bearer token can read any patient the clinician's role allows. A prompt-injected or buggy tool call, or a tampered client patient_id, can pull an unrelated chart and send it to the LLM. Nothing in OpenEMR stops it, and COMP-1 means it wouldn't be traceable.
- **Recommendation:** Register the app with EHR launch and the narrowest patient-bound scopes: `openid fhirUser launch patient/Patient.rs patient/AllergyIntolerance.rs patient/MedicationRequest.rs patient/Condition.rs patient/Observation.rs?category=...|laboratory` and so on, adding one per tool. Take patient_id from the token response `patient` field on the server; ignore any client value. Verify with a test that a users-role token with patient/ scopes gets 403 or empty results for another patient's uuid. Document the scope list as the minimum-necessary policy in AUDIT.md.
- **Verifier correction:** The core claim holds. But the recommended fix (patient/ scopes) has an unstated side effect in this codebase: when a request is patient-bound, the route closures and FhirGenericRestController skip the role's section ACL entirely (routes :75-81, :472-478, :611-619; FhirGenericRestController.php:94-102). Patient/ scopes therefore swap per-role minimum-necessary for per-patient binding (see missed finding 1). The fix needs both patient binding and a restored ACL check, not scopes alone. The 'always-true check' also doesn't affect user/ scopes at all.

#### COMP-6

**The agent's session cache is an unlogged, user-unbound PHI access path with no retention limit** — *high*

- **Evidence:** agent/main.py:120-121 (process-lifetime SESSIONS dict, no TTL or size cap). :144 (session_id may be supplied by the client and isn't required to be a server uuid). :145-147 (lookup by session_id; only patient_id compared). :142-143 (Authorization is only prefix-checked for 'Bearer ' and never validated before the LLM call). :152-153, :164, :170 (tool results and free-text questions are appended to history and replayed). :176 (verify() uses session['fetched'], so replayed claims still pass verification).
- **Impact on the Co-Pilot:** A second user, or a stolen session_id, with any valid bearer token and the same patient_id gets answers built from PHI fetched under the first user's token. OpenEMR logs no FHIR read for the second user, which bypasses both ACL and audit. The PHI also sits in memory indefinitely.
- **Recommendation:** Bind each session to the token subject: store the fhirUser/sub and client_id at creation and return 403 on mismatch. Add a short idle TTL (for example 15 minutes, matching the between-room workflow) plus a max size. Don't persist tool results beyond the request unless needed for verification. Add one pytest case asserting that a different token on an existing session_id gets 403.
- **Verifier correction:** Worse than stated: an attacker doesn't need a valid bearer token. Any string starting with 'Bearer ' plus a known session_id and patient_id reaches the LLM with the cached PHI in history. The model can answer without calling tools (which would 401), and those answers still show verification_passed=true because they are checked against the cached fetched records.

#### COMP-7

**No agent-side audit trail (who asked what about which patient) and no correlation id that reaches OpenEMR** — *high*

- **Evidence:** agent/main.py:63-71 (middleware logs method, path, status and ms, and trusts X-Correlation-ID verbatim at :65, which also allows log-line injection through the formatter at :45). :139-180 (no user, client or scope extraction). :150 (patient_id only in Langfuse metadata). agent/fhir.py:48 (no correlation header). grep -i 'X-Request-ID|correlation' in src/RestControllers and src/Common/Http: no hits. apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php has Provenance at :757-778 and no AuditEvent route, despite FHIR_README.md:367.
- **Impact on the Co-Pilot:** After an incident or complaint, nobody can reconstruct which physician asked about which patient, what data went to the LLM, or which OpenEMR api_log rows belong to that request. Langfuse is an observability tool, not a tamper-resistant audit store, and it shouldn't hold PHI anyway (COMP-3).
- **Recommendation:** Write one append-only JSON audit record per /chat to a store inside the BAA boundary. The simplest option is an INSERT-only table in the same MySQL. Fields: UTC timestamp in ms, a server-generated correlation_id (accept the header only if it matches a uuid regex), session_id, user fhirUser/sub, client_id, a hash of the granted scopes, patient uuid from the launch context, intent enum (not the question text), each FHIR request (resource, non-identifying params, status, count, returned resource ids), model, tokens, latency, and verification pass/fail. Never store the token, question text or answer text. Send X-Correlation-ID on the FHIR calls anyway, and join to api_log on user_id + request_url + timestamp.

#### COMP-M1

**patient/ scopes turn off OpenEMR's role-based ACL for clinician (users-role) tokens, and nothing checks user-to-patient access or ACL when scopes are granted** — *high*

- **Evidence:** Route closures skip RestConfig::request_authorization_check whenever isPatientRequest() is true: apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php:75-81 (AllergyIntolerance), :472-478 (MedicationRequest), :611-619 (Patient), :157-163 (CareTeam). The generic controller does the same: src/RestControllers/FHIR/FhirGenericRestController.php:94-102 (ACL loop runs only in the else branch). isPatientRequest is set purely from scope context: src/Common/Http/HttpRestRouteHandler.php:65-67. The user-to-patient check is a stub: BearerTokenAuthorizationStrategy.php:479-485. The only ACL checks in the OAuth/SMART grant path are src/Common/Auth/AuthUtils.php:580 (admin) and src/RestControllers/SMART/PatientContextSearchController.php:86 (patients/demo, for the standalone patient picker). The default Clinicians group lacks patients/med: library/classes/Installer.class.php:1234.
- **Impact on the Co-Pilot:** If the team follows COMP-4 and switches to patient/ scopes, any OpenEMR user who can open a chart and launch the app (front desk with patients/demo only, a clinician group without patients/med) gets the Co-Pilot reading medications, conditions and allergies that OpenEMR's own ACL would deny them through user/ scopes. That PHI then goes to Anthropic and Langfuse. This breaks minimum necessary by role, and api_log records success=1 (COMP-1).
- **Recommendation:** Don't treat patient/ scopes alone as the minimum-necessary control. Smallest fork patch: in FhirGenericRestController::getAllProcessingResult and the patient-request branches of the routes the agent uses, run the section ACL checks when getRequestUserRole()==='users', even when the request is patient-bound. Alternatively, restrict the Co-Pilot OAuth client to physician users at launch. Add one integration test: a patients/demo-only user with patient/MedicationRequest.rs gets a 403.

#### COMP-5

**Encounter sensitivity restrictions are enforced in the UI but not on FHIR read/search paths** — *medium*

- **Evidence:** UI gates: interface/patient_file/encounter/forms.php:557-565 (form-menu gate) and :699 (form display gate), interface/patient_file/history/encounters.php:507, interface/forms/newpatient/report.php:36. Service check only in updateEncounter: src/Services/EncounterService.php:429, :449-451. Search selects but never filters: EncounterService.php:192, :240. grep 'sensitivit' in src/Services/FHIR and the FHIR route file: no hits. Default ACLs: library/classes/Installer.class.php:1202 gives Physicians (the $doc 'write' ACL at :1206) sensitivities normal+high; :1235 gives Clinicians normal only.
- **Impact on the Co-Pilot:** The Co-Pilot could surface, and send to Anthropic and Langfuse, data from encounters flagged 'high' sensitivity (behavioral health, SUD, etc.) that the same physician can't open in the OpenEMR UI. That breaks the organization's own access policy and possibly 42 CFR Part 2 or state confidentiality laws. It is also exactly the kind of result that erodes clinician trust.
- **Recommendation:** Confirm with a seeded 'high' sensitivity encounter plus a user lacking that sensitivities ACL, calling FHIR Encounter, Condition and DocumentReference. If it's confirmed, the cheapest guard is in the agent: request Encounter first, drop encounter-linked resources whose encounter carries a sensitivity the user lacks, and exclude sensitive categories from the prompt by default. A core fix (a filter in the FHIR encounter-linked services) is a separate, larger change and should be flagged as future work.
- **Verifier correction:** The line citation was incomplete: 557-562 is the menu gate, and the real display gates are at :699 and the other files listed. The impact is also overstated for this project. The target user is a physician, and the default Physicians group already has 'high' sensitivity, so the gap only bites for non-physician users or custom ACLs. The agent's current tools (Patient, AllergyIntolerance, MedicationRequest, from agent/fhir.py:94) aren't encounter-scoped either. It becomes real once Encounter, DocumentReference or encounter-linked Observation tools are added.

#### COMP-8

**Audit log tamper-evidence and retention are weak: unkeyed per-row hashes, an unaudited purge, a 1-2 year default window, ATNA off and fail-silent** — *medium*

- **Evidence:** LogTablesSink.php:63, :83, :87-94 (unkeyed sha3-512, no chain, same schema). interface/reports/audit_log_tamper_report.php:241-255 (per-row recompute); :231-233 does flag a log row deleted while its log_comment_encrypt row remains. EventAuditLogger.php:41-44 (same sqlconf credentials). interface/main/backup.php:68 (admin/super), :1038, :1055-1061. EventAuditLogger.php:418-422 skips 'FROM log ', so the DELETE isn't SQL-audited, but interface/globals.php:849 records the POST to backup.php as http-request-update (script name only; the POSTed form_end_date isn't captured). globals.inc.php:2858-2863 (ATNA default 0). AtnaSink.php:82 ignores the bool. Atna/TcpWriter.php:48-58 (new TLS socket per event, 60s timeout; peer verified only if a CA is set, :36-40). The background_services seed rows (sql/database.sql:209-218) include no log retention or archival job.
- **Impact on the Co-Pilot:** Anyone with the app's DB credentials (which sit in sqlconf.php on the Railway volume) can edit or delete Co-Pilot access rows without detection. Using the built-in purge with defaults drops history well short of the 6-year HIPAA documentation retention, and a breach investigation may find the relevant window gone. Turning ATNA on as written would put a synchronous TLS handshake on every audited query, hurting agent latency.
- **Recommendation:** For the deployment: ship audit rows off-box on a schedule (for example, a nightly mysqldump of log/api_log/extended_log to encrypted object storage with object lock, under a BAA). Set a 6-year retention policy in AUDIT.md and don't use the backup.php purge. Give the agent's audit table a separate INSERT-only DB user. Leave ATNA off unless a receiver exists, and if enabled, fix the fail-silent write and measure latency first. Keyed or chained hashes are future work on the fork.
- **Verifier correction:** 'The purge is never audited' is overstated: the HTTP request audit records that an admin/super user POSTed to backup.php, but not the end date or row count. The tamper report also catches partial deletions where the log_comment_encrypt row survives. The rest stands.

#### COMP-9

**Breach investigation and notification scoping would depend on full-text scans and unreliable IP data** — *medium*

- **Evidence:** library/sanitize.inc.php:29-45 (REMOTE_ADDR plus unverified X-Forwarded-For). sql/database.sql:7760 (log.date datetime), :7774-7775 (log has only the PK and a patient_id index), :92-105 (api_log has only the PK, with no index on log_id, user_id or created_time). agent/main.py:70 (no identity in stdout). SystemLogger.php:62-66 (stderr). For agent traffic, the patient uuid is in api_log.request_url (text), from agent/fhir.py:59,67,81.
- **Impact on the Co-Pilot:** Under 164.404-410, the covered entity has at most 60 days from discovery to notify individuals, HHS and possibly media, and business associates (Anthropic, Langfuse, Railway, the agent operator) must report to the covered entity within their BAA terms. If a Co-Pilot token or Langfuse project is compromised, listing the affected patients means LIKE-scanning longtext across all api_log rows, with no trustworthy source IP and no agent-to-OpenEMR join key. The likely outcome is over-notification or missing the deadline.
- **Recommendation:** Rely on the agent audit record from COMP-7 (patient uuid + user + resource ids per request) as the primary breach-scoping source, and keep it 6 years. Record the physician's client IP and user agent in the agent from the SMART app request, not from OpenEMR's view. Write a one-page incident runbook in AUDIT.md: which stores to query (agent audit table, api_log, Langfuse, Anthropic/Railway support contacts), the BAA notification clauses, and the 60-day clock. Store timestamps in UTC.
- **Verifier correction:** Minor overstatement: for Co-Pilot calls, scoping doesn't require scanning the longtext response bodies. The patient uuid is always in request_url, and user_id is present, so a LIKE scan on request_url plus user_id works, just slowly and without an index. The IP reliability and missing join-key points stand.

#### COMP-M2

**The Co-Pilot is an ONC HTI-1 predictive DSI, and OpenEMR's client registration defaults dsi_type to 'none' with no source attributes** — *medium*

- **Evidence:** src/RestControllers/AuthorizationController.php:366-373: `// default is none` `$dsiTypeName = $params['dsi_type'] ?? ...DSI_TYPE_NONE`, and a DSI service with source attributes is created only if a type is given. sql/database.sql:14128 (`dsi_type` 0=none, 1=evidence-based, 2=predictive). src/Services/DecisionSupportInterventionService.php:15, :18-22 (predictive list and types). sql/database.sql:14705-14708 (predictive source attribute list, e.g. developer and funding). templates/api/smart/dsi-service-questionnaire.json.twig. agent/main.py:23 (an LLM model generates the clinical briefings).
- **Impact on the Co-Pilot:** An LLM that summarizes a chart for clinical decisions is a model-based (predictive) decision support intervention. If it's registered as the default 'none', OpenEMR's DSI source-attribute surface (intended use, developer, validation, risk and fairness details that users can review) stays empty. The AUDIT.md Compliance section would miss the ONC certification hook this codebase already provides, and the FDA non-device CDS rationale (clinicians can independently review the basis, supported by the citation verifier) goes undocumented.
- **Recommendation:** Register the SMART client with dsi_type='predictive' and fill in the predictive source attributes (developer, funding, intended use, known limitations such as the hallucination guard and 'no diagnosis or prescribing'). In AUDIT.md, document the FDA CDS non-device rationale: citations with quoted_value let the physician verify each claim. No code needed; it's a registration parameter plus documentation.

#### COMP-M3

**Every Co-Pilot FHIR call writes several unencrypted audit rows synchronously in-request, including a pre-auth HTTP-request row with a blank user and the patient uuid** — *medium*

- **Evidence:** src/RestControllers/Subscriber/SiteSetupListener.php:132 loads interface/globals.php at kernel.request priority 100 (:37), before AuthorizationListener at priority 50 (AuthorizationListener.php:43). interface/globals.php:847-850 calls EventAuditLogger::logHttpRequest, which writes SCRIPT_NAME plus QUERY_STRING with the session user (still empty at that point): EventAuditLogger.php:700-733; default on at globals.inc.php:2845-2850. apis/.htaccess `RewriteRule (.*) dispatch.php?_REWRITE_COMMAND=$1 [QSA,L]` puts the FHIR path and ?patient=<uuid> into QUERY_STRING. Then each audited SELECT on patient tables writes 2 rows (LogTablesSink.php:60,94; ADODB_mysqli_log.php:47-50; audit_events_query default 1 at globals.inc.php:2832-2837), and the token-use row is written at BearerTokenAuthorizationStrategy.php:316. Ordering and row content are inferred from listener priorities; not run.
- **Impact on the Co-Pilot:** A 3-tool question fans out to many audit INSERTs on a separate DB connection (EventAuditLogger.php:44), most of them inside the request path, and that adds to the physician's 90-second latency budget. The extra rows spread one access across unlinked records (a blank-user row with the patient uuid, and user rows with pid=0), which makes breach scoping and patient-access reports harder, not easier. None of them are encrypted (LogTablesSink.php:89).
- **Recommendation:** For the deployment, measure p95 FHIR latency with audit_events_query on and off, and set api_log_option=1. Keep audit_events_http-request, but document that API rows are pre-auth and user-less. Make the agent-side audit record (COMP-7) the authoritative per-question access log, and state that choice in AUDIT.md.

#### COMP-10

**Accounting of disclosures is manual, and edits or deletions of it are not audited** — *low*

- **Evidence:** interface/patient_file/summary/record_disclosure.php:26-30 (ACL for the manual entry form). EventAuditLogger.php:567-582, :596-615, :622-626 (all through sqlInsertClean_audit). library/sql.inc.php:335-343 (noLog: true). interface/patient_file/summary/disclosure_full.php:45-52 (update keyed by the POSTed disclosure_id, not scoped to pid), :65-70 (delete via GET deletelid). interface/globals.php:849 records these page hits: GET deletelid appears in the query string, and the POST update is recorded without its body.
- **Impact on the Co-Pilot:** Normal Co-Pilot use (treatment, plus business associates acting for the covered entity) is exempt from the 164.528 accounting, so this doesn't block the MVP. Any non-TPO use of agent outputs would need an extended_log entry, though: building eval sets from real charts, research, or sharing briefings externally. Those entries could be silently changed or deleted.
- **Recommendation:** State in AUDIT.md that Co-Pilot reads are treatment/BA uses, not accountable disclosures, and that eval/demo data must be synthetic so no accounting is triggered. If real-chart exports ever happen, record them with recordDisclosure. Flag the unaudited update/delete path as future fork work: route it through the audited connection and scope it by pid.
- **Verifier correction:** 'Not audited' is partly wrong. The SQL isn't audited, but the default HTTP request audit records the delete (including the id in the GET query string) and records that an update POST happened, without its content. As the auditor notes, this doesn't touch Co-Pilot treatment use, so the severity for this project is low.

#### COMP-11

**No de-identification tooling, and optional OpenEMR telemetry can ship FHIR resource uuids to a non-BAA endpoint** — *low*

- **Evidence:** grep 'de_identif|deidentif' over src, library, interface and sql: no PHP or SQL hits (only an unrelated CDA XSD 'deliveryModeIdentifier'). src/RestControllers/Subscriber/TelemetryListener.php:26-31. src/Telemetry/TelemetryService.php:86 strips query strings, :171 is the endpoint, :177 and :202-205 put usageRecords (track_events.event_url, per TelemetryRepository.php:58) in the payload, and :57-70 enable it only when telemetry_disabled=0. sql/database.sql:13237.
- **Impact on the Co-Pilot:** Evals, Langfuse datasets, demo videos and bug reports have no built-in scrubber, so real PHI can leak into non-BAA places by accident. Safe Harbor removes the dates a clinical briefing needs, so de-identifying real charts is impractical for this use case. If someone accepts the telemetry prompt, the agent's FHIR paths (with resource uuids) go to the OpenEMR registration server.
- **Recommendation:** Use Synthea/synthetic patients for all evals, demos and Langfuse datasets. Pseudonymize ids in any external sink with a keyed HMAC (the key lives in Railway variables), not a plain hash. Decline OpenEMR telemetry on the deployment and note it in AUDIT.md.
- **Verifier correction:** Minor: query strings are stripped (TelemetryService.php:86), so only uuids embedded in the path leak, such as the agent's Patient/{id} read (fhir.py:59). A uuid is a pseudonymous identifier, not a direct one. The payload line numbers were slightly off.

#### COMP-12

**Right of access is covered by the portal and FHIR, but agent outputs sit outside the designated record set, and the README overstates audit support** — *low*

- **Evidence:** portal/home.php:337-339 (ccda_alt_service_enable 2 or 3). BearerTokenAuthorizationStrategy.php:225-231, :266-267. sql/database.sql:54 (amendments table). agent/main.py:121, :180. FHIR_README.md:367 against the missing AuditEvent route. library/globals.inc.php:2113-2118 (timeout 7200). src/RestControllers/AuthorizationController.php:110-111 (access token PT1H, refresh token P3M).
- **Impact on the Co-Pilot:** Patients can already get their chart through existing mechanisms. If physicians start acting on or saving Co-Pilot briefings outside OpenEMR (in Langfuse or a notes app), those become decision-support records that patients can't access or amend. The 2-hour idle timeout is long for exam-room workstations where the Co-Pilot panel stays open between patients.
- **Recommendation:** Keep the agent stateless with respect to the record. If a briefing is ever persisted, write it to the chart (for example as a clinical note or DocumentReference through OpenEMR) so it is covered by access and amendment rights. Set the OpenEMR idle timeout to around 15 minutes for the deployment, don't request offline_access for the Co-Pilot, and correct the README claim in AUDIT.md (no FHIR AuditEvent).

