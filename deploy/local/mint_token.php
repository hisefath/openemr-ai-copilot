<?php

/**
 * LOCAL DEV ONLY: mints an OAuth access token for a synthetic demo user, for live evals and load tests without a
 * browser login. Same mechanism as OpenEMR's own `openemr-dev:api-generate-access-token` command
 * (src/Common/Command/GenerateAccessTokenCommand.php) minus its interactive password prompt.
 * Refuses to run unless the site address is localhost, and only for users tagged by seed_demo.php.
 *
 * Usage (inside the local openemr container):
 *   su-exec apache php mint_token.php <client_id> <username> [patient_uuid]
 * Prints the bearer token response JSON. Tokens last 1 hour and carry no refresh token.
 */

$_GET['site'] = 'default';
$ignoreAuth = 1;
require_once '/var/www/localhost/htdocs/openemr/interface/globals.php';

use League\OAuth2\Server\CryptKey;
use League\OAuth2\Server\ResponseTypes\BearerTokenResponse;
use OpenEMR\Common\Auth\OAuth2KeyConfig;
use OpenEMR\Common\Auth\OpenIDConnect\Entities\ScopeEntity;
use OpenEMR\Common\Auth\OpenIDConnect\Repositories\AccessTokenRepository;
use OpenEMR\Common\Auth\OpenIDConnect\Repositories\ClientRepository;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Http\Psr17Factory;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\FHIR\Config\ServerConfig;
use OpenEMR\Services\TrustedUserService;
use Symfony\Component\HttpFoundation\Session\Session;
use Symfony\Component\HttpFoundation\Session\Storage\MockFileSessionStorage;

const DEMO_TAG = 'AgentForge demo';
$SCOPES = ['openid', 'fhirUser', 'user/Patient.rs', 'user/AllergyIntolerance.rs', 'user/MedicationRequest.rs',
    'user/Condition.rs', 'user/Observation.rs', 'user/Encounter.rs', 'user/Appointment.rs'];

[, $clientId, $username, $patient] = array_pad($argv, 4, null);
$site = (string) OEGlobalsBag::getInstance()->get('site_addr_oath');
if (!preg_match('#^http://(localhost|127\.0\.0\.1)(:\d+)?$#', $site)) {
    fwrite(STDERR, "refusing: local development only (site_addr_oath must be http://localhost)\n");
    exit(2);
}
$user = QueryUtils::fetchRecords('SELECT uuid FROM users WHERE username = ? AND info = ? AND active = 1', [$username, DEMO_TAG])[0] ?? null;
$client = $clientId ? (new ClientRepository())->getClientEntity($clientId) : null;
if (!$user || !$client || !$client->isEnabled()) {
    fwrite(STDERR, "usage: mint_token.php <enabled client_id> <seeded demo username> [patient_uuid]\n");
    exit(1);
}

$scopes = array_map(ScopeEntity::createFromString(...), $patient ? [...$SCOPES, 'launch/patient'] : $SCOPES);
$session = new Session(new MockFileSessionStorage());
$session->set('trusted', 1);
$repo = new AccessTokenRepository(new ServerConfig(), $session);
$token = $repo->getNewToken($client, $scopes);
if ($patient) {
    $repo->setContextForNewTokens(['patient' => $patient]);
}
$token->setExpiryDateTime((new DateTimeImmutable())->add(new DateInterval('PT1H')));
$keys = new OAuth2KeyConfig(OEGlobalsBag::getInstance()->get('OE_SITE_DIR'));
$keys->configKeyPairs();
$privateKey = new CryptKey($keys->getPrivateKeyLocation(), $keys->getPassPhrase());
$token->setPrivateKey($privateKey);
$token->setUserIdentifier(\OpenEMR\Common\Uuid\UuidRegistry::uuidToString($user['uuid']));
$token->setIdentifier(bin2hex(random_bytes(40)));
$repo->persistNewAccessToken($token);
(new TrustedUserService())->saveTrustedUser($client->getIdentifier(), $token->getUserIdentifier(),
    array_map(fn($s) => $s->getIdentifier(), $scopes), 1, '', json_encode($session->all()), 'password_grant');

$response = new BearerTokenResponse();
$response->setEncryptionKey($keys->getEncryptionKey());
$response->setAccessToken($token);
$response->setPrivateKey($privateKey);
$http = $response->generateHttpResponse((new Psr17Factory())->createResponse());
$http->getBody()->rewind();
echo $http->getBody()->getContents(), "\n";
