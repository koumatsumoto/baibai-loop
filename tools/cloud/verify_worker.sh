#!/usr/bin/env bash
set -euo pipefail

base_url="${WORKER_BASE_URL:-https://baibai-loop.koumatsumoto.workers.dev}"
ticker="${VERIFY_TICKER:-7203}"
verification_dir="$(mktemp -d /tmp/baibai-worker-verify.XXXXXX)"

cleanup() {
  case "${verification_dir}" in
    /tmp/baibai-worker-verify.*) rm -r -- "${verification_dir}" ;;
    *) printf 'refusing to remove unexpected verification path: %s\n' "${verification_dir}" >&2 ;;
  esac
}
trap cleanup EXIT

if [[ ! "${base_url}" =~ ^https://[^/]+$ ]]; then
  printf 'WORKER_BASE_URL must be an HTTPS origin without a trailing slash\n' >&2
  exit 2
fi
if [[ ! "${ticker}" =~ ^[0-9A-Z]{4}$ ]]; then
  printf 'VERIFY_TICKER must be a four-character uppercase ticker\n' >&2
  exit 2
fi

# The password is typed, never passed as an argument or read from a file, so it stays
# out of shell history and process listings. Without a terminal the read reaches EOF and
# `set -e` would end the script before it sends a single request: an empty run must not
# be mistaken for a passing verification.
if [[ ! -t 0 ]]; then
  printf 'VIEW_PASSWORD must be typed at a terminal; run this from an interactive shell\n' >&2
  exit 2
fi
read -r -s -p 'VIEW_PASSWORD: ' password
printf '\n'
if [[ -z "${password}" ]]; then
  printf 'VIEW_PASSWORD must not be empty\n' >&2
  exit 2
fi

request() {
  local path="$1"
  local token="$2"
  local label="$3"
  local expected_status="$4"
  local slug status headers
  slug="$(printf '%s' "${path}" | tr -c '[:alnum:]' '_')"
  headers="${verification_dir}/${label}-${slug}.headers"
  if [[ -n "${token}" ]]; then
    status="$(
      printf 'header = "Authorization: Bearer %s"\n' "${token}" |
        curl --config - --silent --show-error \
          --dump-header "${headers}" --output /dev/null \
          --write-out '%{http_code}' "${base_url}${path}"
    )"
  else
    status="$(
      curl --silent --show-error \
        --dump-header "${headers}" --output /dev/null \
        --write-out '%{http_code}' "${base_url}${path}"
    )"
  fi

  if [[ "${status}" != "${expected_status}" ]]; then
    printf '%s %s: expected HTTP %s, got %s\n' \
      "${label}" "${path}" "${expected_status}" "${status}" >&2
    return 1
  fi
  if ! awk 'BEGIN { IGNORECASE=1; found=0 } /^cache-control:[[:space:]]*no-store\r?$/ { found=1 } END { exit !found }' "${headers}"; then
    printf '%s %s: Cache-Control: no-store is missing\n' "${label}" "${path}" >&2
    return 1
  fi
  if ! awk 'BEGIN { IGNORECASE=1; found=0 } /^strict-transport-security:/ { found=1 } END { exit !found }' "${headers}"; then
    printf '%s %s: Strict-Transport-Security is missing\n' "${label}" "${path}" >&2
    return 1
  fi
  if awk 'BEGIN { IGNORECASE=1 } /^access-control-allow-origin:/ { found=1 } END { exit !found }' "${headers}"; then
    printf '%s %s: unexpected CORS response header\n' "${label}" "${path}" >&2
    return 1
  fi
}

paths=(
  /api/health
  /api/dashboard
  /api/screening/latest
  /api/screening/history
  /api/operations
  /api/meta
  /api/macro/reading
  "/api/securities/${ticker}"
)
for period in 1y 5y 10y max; do
  for granularity in daily weekly monthly yearly; do
    paths+=("/api/macro?period=${period}&granularity=${granularity}")
  done
done

# Keyed routes, checked with a well-formed key that serving does not carry. The
# credential still decides the response, and a correct one resolves to a clean 404, so
# every route the Worker answers is exercised without depending on which keys exist.
absent_key_paths=(
  /api/screening/history/2000-01-01
  /api/macro/context/no-such-context
  /api/securities/ZZZZ
)

for path in "${paths[@]}"; do
  request "${path}" '' missing 401
  request "${path}" 'definitely-not-the-view-password' wrong 401
  request "${path}" "${password}" correct 200
done

for path in "${absent_key_paths[@]}"; do
  request "${path}" '' missing 401
  request "${path}" 'definitely-not-the-view-password' wrong 401
  request "${path}" "${password}" correct 404
done

http_status="$(curl --silent --show-error --max-redirs 0 --output /dev/null --write-out '%{http_code}' "${base_url/https:/http:}/")"
if [[ "${http_status}" != 308 ]]; then
  printf 'HTTP origin: expected redirect 308, got %s\n' "${http_status}" >&2
  exit 1
fi

printf 'verified %s API routes with missing, wrong, and correct credentials\n' \
  "$((${#paths[@]} + ${#absent_key_paths[@]}))"
