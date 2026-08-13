#!/bin/sh
# Mint the throwaway CA and server certificates the TLS rig runs against.
#
# Nothing here is committed: a private key in a public repository is a private
# key everyone has. Run this once before `docker compose --profile tls up -d`,
# and again whenever the certificates expire.
#
# Three certificates, because the rig's negative arms need to be the wrong peer
# in three specific ways (see test/tls.scrl):
#
#   ca.crt/key      a throwaway root, trusted only via SSL_CERT_FILE
#   server.crt/key  SAN DNS:localhost — what the passing arm connects by
#   ipsan.crt/key   SAN IP:127.0.0.1,DNS:localhost — kept because "does an IP
#                   SAN verify at all" is a question the docs get wrong; see
#                   the note in test/tls.scrl.
#
# `server.crt` deliberately carries NO IP SAN, which is what makes connecting
# to 127.0.0.1 fail as HostnameMismatch rather than succeed.

set -eu

dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/test/tls"
mkdir -p "$dir"
cd "$dir"

days=3650

openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt \
	-days "$days" -subj "/CN=scarlet redis_client test CA" 2>/dev/null

leaf() {
	name="$1"
	san="$2"
	openssl req -newkey rsa:2048 -nodes -keyout "${name}.key" -out "${name}.csr" \
		-subj "/CN=localhost" 2>/dev/null
	printf 'subjectAltName=%s\n' "$san" > "${name}.ext"
	openssl x509 -req -in "${name}.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
		-out "${name}.crt" -days "$days" -extfile "${name}.ext" 2>/dev/null
	rm -f "${name}.csr" "${name}.ext"
}

leaf server "DNS:localhost"
leaf ipsan "IP:127.0.0.1,DNS:localhost"

# Redis reads the key as the user inside the container; the certificates are
# public and the keys are throwaway, but there is no reason to leave them
# world-readable.
chmod 644 ca.crt server.crt ipsan.crt
chmod 644 ca.key server.key ipsan.key

echo "wrote $dir:"
ls -1 "$dir"
