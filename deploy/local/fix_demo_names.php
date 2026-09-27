<?php

/**
 * Strip Synthea's numeric suffixes from demo patient names.
 *
 * The synthetic dataset generates names like "Farah862 Santa373 Steuber698" — the digits are Synthea's
 * uniqueness counters, not part of the name. They are meaningless on screen and they make the calendar and
 * the Co-Pilot's citations read like corrupted data in a demo.
 *
 * Only touches digit runs in fname / mname / lname, never any other column. Idempotent: a name with no
 * digits in it is left exactly as it is, so re-running changes nothing. Synthetic demo data only.
 *
 * Run: sh deploy/remote_seed.sh fix_demo_names.php
 */
$_GET['site'] = 'default';
$ignoreAuth = 1;
require_once '/var/www/localhost/htdocs/openemr/interface/globals.php';

use OpenEMR\Common\Database\QueryUtils;

$rows = QueryUtils::fetchRecords('SELECT pid, fname, mname, lname FROM patient_data', []);

$changed = [];
foreach ($rows as $r) {
    $new = [];
    foreach (['fname', 'mname', 'lname'] as $f) {
        // Every digit run, not just a trailing one: Synthea sometimes packs the middle name into fname
        // ("Blanca837 Tanesha560"), so a $-anchored pattern only cleans the last token. Collapse any
        // double space the removal leaves behind.
        $v = preg_replace('/\d+/', '', (string)($r[$f] ?? ''));
        $new[$f] = trim(preg_replace('/\s{2,}/', ' ', $v));
    }
    if ($new['fname'] === $r['fname'] && $new['mname'] === $r['mname'] && $new['lname'] === $r['lname']) {
        continue;
    }
    QueryUtils::sqlStatementThrowException(
        'UPDATE patient_data SET fname = ?, mname = ?, lname = ? WHERE pid = ?',
        [$new['fname'], $new['mname'], $new['lname'], $r['pid']]
    );
    $changed[] = trim($r['fname'] . ' ' . $r['lname']) . '  ->  ' . trim($new['fname'] . ' ' . $new['lname']);
}

// Today's schedule, so the calendar can be eyeballed straight after.
$today = date('Y-m-d');
$appts = QueryUtils::fetchRecords(
    "SELECT e.pc_startTime AS t, p.fname, p.lname, e.pc_apptstatus AS s
       FROM openemr_postcalendar_events e
       JOIN patient_data p ON p.pid = e.pc_pid
      WHERE e.pc_eventDate = ? ORDER BY e.pc_startTime",
    [$today]
);
$schedule = [];
foreach ($appts as $a) {
    $schedule[] = substr((string)$a['t'], 0, 5) . '  ' . trim($a['fname'] . ' ' . $a['lname'])
        . '  [' . $a['s'] . ']';
}

echo json_encode([
    'patients_renamed' => count($changed),
    'examples' => array_slice($changed, 0, 5),
    'todays_schedule' => $schedule,
], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), "\n";
