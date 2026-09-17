<?php

/**
 * Dev utility: dumps real FHIR R4 resources for a few synthetic patients from a LOCAL OpenEMR,
 * by calling OpenEMR's own FHIR service classes (the same ones the REST API uses).
 * Read-only. Synthetic (Synthea) data only. Output: one JSON object per patient on stdout.
 *
 * Run inside the local openemr container:
 *   su-exec apache php dump_fhir_fixtures.php
 */

$_GET['site'] = 'default';
$ignoreAuth = 1;
require_once '/var/www/localhost/htdocs/openemr/interface/globals.php';

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Services\FHIR\FhirAllergyIntoleranceService;
use OpenEMR\Services\FHIR\FhirConditionService;
use OpenEMR\Services\FHIR\FhirEncounterService;
use OpenEMR\Services\FHIR\FhirMedicationRequestService;
use OpenEMR\Services\FHIR\FhirObservationService;
use OpenEMR\Services\FHIR\FhirPatientService;

// Pick patients that exercise edge cases: most allergies, no allergies, most lab results.
$picks = [
    'most_allergies' => "SELECT p.pid FROM patient_data p JOIN lists l ON l.pid = p.pid AND l.type = 'allergy'
                          GROUP BY p.pid ORDER BY COUNT(*) DESC, p.pid LIMIT 1",
    'no_allergies'   => "SELECT p.pid FROM patient_data p LEFT JOIN lists l ON l.pid = p.pid AND l.type = 'allergy'
                          WHERE l.id IS NULL ORDER BY p.pid LIMIT 1",
    'most_labs'      => "SELECT po.patient_id AS pid FROM procedure_order po
                          JOIN procedure_report pr ON pr.procedure_order_id = po.procedure_order_id
                          JOIN procedure_result r ON r.procedure_report_id = pr.procedure_report_id
                          GROUP BY po.patient_id ORDER BY COUNT(*) DESC, po.patient_id LIMIT 1",
];

$bundle = static function ($processingResult): array {
    $entries = [];
    foreach ($processingResult->getData() as $resource) {
        $entries[] = ['resource' => json_decode(json_encode($resource), true)];
    }
    return ['resourceType' => 'Bundle', 'type' => 'searchset', 'total' => count($entries), 'entry' => $entries];
};

$out = [];
foreach ($picks as $label => $sql) {
    $pid = QueryUtils::fetchSingleValue($sql, 'pid');
    if ($pid === null) {
        continue;
    }
    $uuid = UuidRegistry::uuidToString(QueryUtils::fetchSingleValue('SELECT uuid FROM patient_data WHERE pid = ?', 'uuid', [$pid]));
    $out[$label] = [
        'patient_uuid' => $uuid,
        'Patient' => $bundle((new FhirPatientService())->getAll(['_id' => $uuid])),
        'AllergyIntolerance' => $bundle((new FhirAllergyIntoleranceService())->getAll(['patient' => $uuid])),
        'MedicationRequest' => $bundle((new FhirMedicationRequestService())->getAll(['patient' => $uuid])),
        'Condition' => $bundle((new FhirConditionService())->getAll(['patient' => $uuid])),
        'Observation_laboratory' => $bundle((new FhirObservationService())->getAll(['patient' => $uuid, 'category' => 'laboratory'])),
        'Observation_vital_signs' => $bundle((new FhirObservationService())->getAll(['patient' => $uuid, 'category' => 'vital-signs'])),
        'Encounter' => $bundle((new FhirEncounterService())->getAll(['patient' => $uuid])),
    ];
}
echo json_encode($out, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), "\n";
