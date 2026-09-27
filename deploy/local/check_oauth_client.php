<?php

/**
 * READ-ONLY diagnostic: is the registered SMART client enabled, and what scopes does it carry?
 *
 * A launch that fails with `token_http_401` means OpenEMR's token endpoint rejected our client
 * credentials once the request was well formed — which is what a registered-but-not-enabled client
 * looks like from outside. This answers that without guessing.
 *
 * Never prints the client secret. Demo/synthetic environment only.
 *
 * Run: sh deploy/remote_seed.sh check_oauth_client.php
 */
$_GET['site'] = 'default';
$ignoreAuth = 1;
require_once '/var/www/localhost/htdocs/openemr/interface/globals.php';

use OpenEMR\Common\Database\QueryUtils;

$rows = QueryUtils::fetchRecords(
    "SELECT client_id, client_name, is_enabled, client_role, is_confidential, scope
       FROM oauth_clients ORDER BY client_name",
    []
);

$out = [];
foreach ($rows as $r) {
    $scope = (string)($r['scope'] ?? '');
    $out[] = [
        'client_id'    => substr((string)$r['client_id'], 0, 12) . '…',
        'name'         => $r['client_name'],
        'ENABLED'      => ((int)$r['is_enabled'] === 1) ? 'YES' : 'NO  <-- a launch against this returns 401',
        'role'         => $r['client_role'],
        'confidential' => ((int)$r['is_confidential'] === 1) ? 'yes' : 'no',
        'n_scopes'     => $scope === '' ? 0 : count(preg_split('/\s+/', trim($scope))),
        'has_api_oemr' => str_contains($scope, 'api:oemr') ? 'yes' : 'no',
        'has_doc_write' => str_contains($scope, 'user/document.crs') ? 'yes' : 'no',
    ];
}
echo json_encode(['clients' => $out], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), "\n";
