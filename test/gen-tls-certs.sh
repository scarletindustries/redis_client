#!/bin/sh
# Mint the throwaway CA and the leaf certificate the TLS listener serves.
#
#	sh test/gen-tls-certs.sh [dir]
#
# `docker compose up -d` runs this in a container before starting `redis-tls`,
# so nothing here may assume a host has openssl; the argument exists so it can
# also be run by hand against the same directory.
#
# The leaf's SAN is `localhost` and nothing else. The Scarlet TLS client
# verifies by name and refuses an IP literal outright, so a certificate for
# 127.0.0.1 would not be a working shortcut — it would be unusable.
#
# Existing certificates are left alone: the CA the trust step installed has to
# stay the one the server presents, and regenerating on every `up` would break
# that the second time round. Delete the directory to start over.
set -eu

dir=${1:-/certs}
mkdir -p "$dir"
cd "$dir"

if [ -f ca.crt ] && [ -f server.crt ] && [ -f server.key ]; then
	echo "certificates already present in $dir, leaving them alone"
	exit 0
fi

openssl genrsa -out ca.key 4096
openssl req -x509 -new -sha256 -key ca.key -days 3650 \
	-subj '/CN=redis_client test ca' -out ca.crt

openssl genrsa -out server.key 2048
openssl req -new -sha256 -key server.key -subj '/CN=localhost' -out server.csr

# Written out rather than passed with -addext: LibreSSL, which is what
# /usr/bin/openssl is on macOS, does not take -addext on `x509 -req`.
cat > server.ext <<-EOF
	basicConstraints = CA:FALSE
	extendedKeyUsage = serverAuth
	subjectAltName = DNS:localhost
EOF

openssl x509 -req -sha256 -in server.csr -CA ca.crt -CAkey ca.key \
	-CAcreateserial -days 3650 -extfile server.ext -out server.crt

rm -f server.csr server.ext ca.srl
# The container runs as root and redis-server does not; the private key it
# reads has to be readable by that user, and this CA guards nothing.
chmod 644 ca.crt server.crt server.key
chmod 600 ca.key

echo "wrote $dir/ca.crt and $dir/server.crt (SAN localhost, 3650 days)"
