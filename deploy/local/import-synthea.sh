#!/bin/sh
# Imports pre-generated Synthea CCDA files into the local OpenEMR container using
# OpenEMR's own importer (same steps as importRandomPatients in the image's
# /root/devtoolsLibrary.source, minus generating inside the 1GB-limited container).
#
# Generate first (demo/synthetic data only; fixed seed = reproducible):
#   java -Xmx4g -jar synthea-with-dependencies.jar -s 20260917 -cs 20260917 \
#     --exporter.fhir.export false --exporter.ccda.export true \
#     --generate.only_alive_patients true -p 25
#
# Usage: ./import-synthea.sh /path/to/synthea/output/ccda
set -eu
SRC=${1:?usage: import-synthea.sh <ccda dir>}
[ -n "$(ls -A "$SRC"/*.xml 2>/dev/null)" ] || { echo "no .xml files in $SRC" >&2; exit 1; }
cd "$(dirname "$0")"
DC="docker-compose -f compose.yml"
OE=/var/www/localhost/htdocs/openemr

# import_ccda.php in dev mode truncates the CCDA staging tables; refuse unless they are empty.
staging=$($DC exec -T openemr sh -c 'mariadb --host=mysql --user="$MYSQL_USER" --password="$MYSQL_PASS" \
  --ssl-ca='"$OE"'/sites/default/documents/certificates/mysql-ca --skip-column-names --batch "$MYSQL_DATABASE" \
  -e "SELECT (SELECT COUNT(*) FROM audit_master) + (SELECT COUNT(*) FROM audit_details)"')
[ "$staging" = 0 ] || { echo "CCDA staging tables not empty ($staging rows); refusing to import" >&2; exit 1; }

$DC exec -T openemr sh -c 'mkdir -p /tmp/ccda-import && chmod 1777 /tmp/ccda-import'
tar -C "$SRC" -cf - . | $DC exec -T openemr tar -C /tmp/ccda-import -xf -
$DC exec -T openemr sh -c "chmod -R a+rX /tmp/ccda-import && \
  sed -i 's@exit;@//exit;@' $OE/contrib/util/ccda_import/import_ccda.php && \
  OPENEMR_ENABLE_CCDA_IMPORT=1 su-exec apache php $OE/contrib/util/ccda_import/import_ccda.php \
    --sourcePath=/tmp/ccda-import --site=default --openemrPath=$OE --isDev=true; rc=\$?; \
  sed -i 's@//exit;@exit;@' $OE/contrib/util/ccda_import/import_ccda.php; exit \$rc"
