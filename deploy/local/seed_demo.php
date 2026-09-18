<?php

/**
 * Seeds LOCAL demo data for the Clinical Co-Pilot (synthetic only):
 *  - three users, one per role the evals need: physician (Physicians group, NPI set so OpenEMR
 *    identifies her as Practitioner/<uuid>), clinician (Clinicians), front office (Front Office);
 *  - today's appointments for the physician across synthetic patients, including cancelled and
 *    no-show entries that the schedule scan must exclude.
 * Idempotent: existing demo users are left untouched; today's demo appointments are not duplicated.
 * Passwords are random and printed once as JSON on stdout (store them outside git).
 *
 * Run inside the local openemr container:  su-exec apache php seed_demo.php
 */

$_GET['site'] = 'default';
$ignoreAuth = 1;
require_once '/var/www/localhost/htdocs/openemr/interface/globals.php';

use OpenEMR\Common\Acl\AclExtended;
use OpenEMR\Common\Auth\AuthHash;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;

const DEMO_TAG = 'AgentForge demo';

$users = [
    ['username' => 'dr_chen', 'fname' => 'Maya', 'lname' => 'Chen', 'group' => 'Physicians', 'authorized' => 1, 'calendar' => 1, 'npi' => '1234567893'],
    ['username' => 'nurse_ortiz', 'fname' => 'Luis', 'lname' => 'Ortiz', 'group' => 'Clinicians', 'authorized' => 0, 'calendar' => 0, 'npi' => ''],
    ['username' => 'front_lee', 'fname' => 'Dana', 'lname' => 'Lee', 'group' => 'Front Office', 'authorized' => 0, 'calendar' => 0, 'npi' => ''],
];

$facilityId = (int) QueryUtils::fetchSingleValue('SELECT id FROM facility ORDER BY id LIMIT 1', 'id');
$created = [];
foreach ($users as $u) {
    if (QueryUtils::fetchSingleValue('SELECT id FROM users WHERE username = ?', 'id', [$u['username']])) {
        $created[$u['username']] = ['status' => 'exists'];
        continue;
    }
    $password = rtrim(strtr(base64_encode(random_bytes(18)), '+/', 'Ab'), '=') . '!7a';
    $uuid = UuidRegistry::getRegistryForTable('users')->createUuid();
    $id = QueryUtils::sqlInsert(
        'INSERT INTO users (uuid, username, password, authorized, active, fname, lname, facility_id, facility, calendar, npi, see_auth, info)
         VALUES (?, ?, ?, ?, 1, ?, ?, ?, (SELECT name FROM facility WHERE id = ?), ?, ?, 1, ?)',
        [$uuid, $u['username'], 'NoLongerUsed', $u['authorized'], $u['fname'], $u['lname'], $facilityId, $facilityId, $u['calendar'], $u['npi'], DEMO_TAG]
    );
    $hash = (new AuthHash())->passwordHash($password);
    QueryUtils::sqlStatementThrowException(
        'INSERT INTO users_secure (id, username, password, last_update_password) VALUES (?, ?, ?, NOW())',
        [$id, $u['username'], $hash]
    );
    QueryUtils::sqlStatementThrowException('INSERT INTO `groups` (name, user) VALUES (?, ?)', ['Default', $u['username']]);
    AclExtended::setUserAro([$u['group']], $u['username'], $u['fname'], '', $u['lname']);
    $created[$u['username']] = ['status' => 'created', 'password' => $password, 'group' => $u['group'], 'uuid' => UuidRegistry::uuidToString($uuid)];
}

// Today's schedule for dr_chen: 10 patients, varied statuses. FHIR maps '-'->proposed, '@'->arrived,
// '*'->booked, '^'->pending, 'x'->cancelled, '?'->noshow (src/Services/FHIR/FhirAppointmentService.php).
$drId = (int) QueryUtils::fetchSingleValue('SELECT id FROM users WHERE username = ?', 'id', ['dr_chen']);
$today = date('Y-m-d');
$already = (int) QueryUtils::fetchSingleValue(
    'SELECT COUNT(*) AS n FROM openemr_postcalendar_events WHERE pc_aid = ? AND pc_eventDate = ? AND pc_hometext = ?',
    'n',
    [$drId, $today, DEMO_TAG]
);
$appointments = 0;
if ($already === 0) {
    $pids = QueryUtils::fetchTableColumn('SELECT pid FROM patient_data ORDER BY pid LIMIT 10', 'pid');
    $statuses = ['-', '@', '*', '-', '^', '-', 'x', '-', '?', '*'];
    foreach ($pids as $i => $pid) {
        $start = sprintf('%02d:%02d:00', 8 + intdiv(30 + $i * 20, 60), (30 + $i * 20) % 60);
        QueryUtils::sqlInsert(
            // pc_sharing = 3 (SHARING_GLOBAL): with no provider selected, the calendar only shows events whose
            // pc_aid is the logged-in user's or that are globally shared (pnuserapi.php pcQueryEvents). Demo data has
            // to be visible to whoever opens it — a grader signing in as admin, not just dr_chen.
            'INSERT INTO openemr_postcalendar_events
               (uuid, pc_catid, pc_aid, pc_pid, pc_title, pc_time, pc_hometext, pc_eventDate, pc_endDate, pc_duration,
                pc_recurrtype, pc_startTime, pc_endTime, pc_alldayevent, pc_apptstatus, pc_eventstatus, pc_facility,
                pc_billing_location, pc_sharing)
             VALUES (?, 5, ?, ?, ?, NOW(), ?, ?, ?, 900, 0, ?, ADDTIME(?, "00:15:00"), 0, ?, 1, ?, ?, 3)',
            [UuidRegistry::getRegistryForTable('openemr_postcalendar_events')->createUuid(), $drId, $pid, 'Office Visit',
             DEMO_TAG, $today, $today, $start, $start, $statuses[$i], $facilityId, $facilityId]
        );
        $appointments++;
    }
}

echo json_encode(['users' => $created, 'appointments_created' => $appointments, 'date' => $today], JSON_PRETTY_PRINT), "\n";
