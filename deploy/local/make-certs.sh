#!/bin/sh
# Generates a local CA and a MySQL server cert for hostname "mysql" so the local
# stack uses verified TLS between OpenEMR and MySQL, like production on Railway.
# Output goes to ./certs (gitignored). Refuses to overwrite existing files.
set -eu
cd "$(dirname "$0")"
mkdir -p certs
for f in certs/ca.pem certs/ca-key.pem certs/server-cert.pem certs/server-key.pem; do
    [ ! -e "$f" ] || { echo "certs already exist, nothing to do"; exit 0; }
done
umask 077
openssl req -x509 -newkey rsa:3072 -nodes -days 825 -subj "/CN=AgentForge Local MySQL CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout certs/ca-key.pem -out certs/ca.pem 2>/dev/null
openssl req -newkey rsa:3072 -nodes -subj "/CN=mysql" -keyout certs/server-key.pem -out certs/server.csr 2>/dev/null
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:mysql,DNS:localhost,IP:127.0.0.1\n' > certs/server.ext
openssl x509 -req -in certs/server.csr -CA certs/ca.pem -CAkey certs/ca-key.pem -CAcreateserial \
    -days 825 -extfile certs/server.ext -out certs/server-cert.pem 2>/dev/null
# The CA and server cert are public; the mysql container (uid 999) must read the server key.
chmod 644 certs/ca.pem certs/server-cert.pem certs/server-key.pem
openssl verify -CAfile certs/ca.pem certs/server-cert.pem
