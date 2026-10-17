#!/usr/bin/env bash
# Post-deploy checks. Usage: deploy/check.sh https://anki.panzeilkamp.com
# Needs curl; the certificate and port checks also use openssl and only run for a public https URL.
# Reads no secrets and sends none.
set -euo pipefail

url="${1:?usage: check.sh https://host}"
url="${url%/}"
host="${url#*://}"; host="${host%%/*}"; host="${host%%:*}"
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok: $*"; }

headers="$(curl -sS -i -X POST --max-time 10 "$url/mcp" -o /dev/null -D - || true)"
echo "$headers" | head -1 | grep -q " 401" || fail "POST /mcp without a token did not return 401"
echo "$headers" | grep -qi '^www-authenticate:.*resource_metadata=' || fail "401 has no WWW-Authenticate resource_metadata"
ok "unauthenticated POST /mcp is 401 with WWW-Authenticate"

for path in /.well-known/oauth-protected-resource/mcp /.well-known/oauth-authorization-server; do
  body="$(curl -sS --max-time 10 -f "$url$path")" || fail "$path is not served"
  echo "$body" | grep -q "$url" || fail "$path does not name $url"
  ok "$path names $url"
done

case "$url" in
  https://*)
    issuer="$(echo | openssl s_client -connect "$host:443" -servername "$host" 2>/dev/null | openssl x509 -noout -issuer)" \
      || fail "could not read the certificate"
    echo "$issuer" | grep -qi "let's encrypt\|letsencrypt" || fail "certificate issuer is not Let's Encrypt: $issuer"
    ok "certificate issued by Let's Encrypt"
    if curl -sS --max-time 5 -o /dev/null "http://$host:8765/" 2>/dev/null; then
      fail "port 8765 is reachable from here; it must listen on loopback only"
    fi
    ok "port 8765 is not reachable"
    ;;
esac
echo "all checks passed"
