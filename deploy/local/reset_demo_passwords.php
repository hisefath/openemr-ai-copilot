<?php

/**
 * Rotates the passwords of the seeded demo users (synthetic data only).
 *
 * seed_demo.php prints each password once, at creation, and is idempotent afterwards — re-running it leaves
 * existing users untouched, so there is no way through it to change a password that has been shared and needs
 * replacing. This does that one job.
 *
 * Only touches users tagged `AgentForge demo` by seed_demo.php: it cannot rotate `admin`, or any account a real
 * deployment created. Same generator and same hash path as seed_demo.php, so rotated accounts are
 * indistinguishable from freshly seeded ones.
 *
 * Prints the new passwords once, as JSON on stdout. They are not written anywhere — capture them here or run it
 * again. Store them outside git.
 *
 * Run inside the openemr container:            su-exec apache php reset_demo_passwords.php [username ...]
 * Against the deployed instance:               sh deploy/remote_seed.sh reset_demo_passwords.php
 * With no arguments it rotates every demo user.
 */

$_GET['site'] = 'default';
$ignoreAuth = 1;
require_once '/var/www/localhost/htdocs/openemr/interface/globals.php';

use OpenEMR\Common\Auth\AuthHash;
use OpenEMR\Common\Database\QueryUtils;

const DEMO_TAG = 'AgentForge demo';

$only = array_slice($argv, 1);
$rows = QueryUtils::fetchRecords('SELECT id, username FROM users WHERE info = ? AND active = 1', [DEMO_TAG]);
if (!$rows) {
    fwrite(STDERR, "no demo users found (seed_demo.php tags them '" . DEMO_TAG . "')\n");
    exit(1);
}

$out = [];
foreach ($rows as $row) {
    if ($only && !in_array($row['username'], $only, true)) {
        $out[$row['username']] = ['status' => 'skipped'];
        continue;
    }
    // Same generator as seed_demo.php: 18 random bytes, base64 with +/ mapped away, plus a suffix that satisfies
    // OpenEMR's default password policy (a digit, a symbol and a lowercase letter).
    $password = rtrim(strtr(base64_encode(random_bytes(18)), '+/', 'Ab'), '=') . '!7a';
    $hash = (new AuthHash())->passwordHash($password);
    $n = QueryUtils::sqlStatementThrowException(
        'UPDATE users_secure SET password = ?, last_update_password = NOW() WHERE id = ? AND username = ?',
        [$hash, $row['id'], $row['username']]
    );
    // A demo user with no users_secure row could never have logged in; create one rather than silently no-op.
    if (!QueryUtils::fetchSingleValue('SELECT id FROM users_secure WHERE id = ?', 'id', [$row['id']])) {
        QueryUtils::sqlStatementThrowException(
            'INSERT INTO users_secure (id, username, password, last_update_password) VALUES (?, ?, ?, NOW())',
            [$row['id'], $row['username'], $hash]
        );
    }
    $out[$row['username']] = ['status' => 'rotated', 'password' => $password];
}

echo json_encode($out, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), "\n";
