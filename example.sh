#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${BASE_URL:-http://localhost:8000}"
SESSION_ID="demo-$(date +%s)"
TEAM_SESSION_ID="demo-team-$(date +%s)"
REPO_PATH="${REPO_PATH:-$(pwd)}"
DRY_RUN="${DRY_RUN:-false}"
LITELLM_SSL_VERIFY="${LITELLM_SSL_VERIFY:-false}"
LITELLM_CA_BUNDLE="${LITELLM_CA_BUNDLE:-}"
TEAM_AGENT="${TEAM_AGENT:-team}"
TEAM_DEMO_ENABLED="${TEAM_DEMO_ENABLED:-true}"
GRAPH_MODEL="${GRAPH_MODEL:-}"
GRAPH_MAX_RETRIES="${GRAPH_MAX_RETRIES:-3}"
GRAPH_RETRY_DELAY_SECONDS="${GRAPH_RETRY_DELAY_SECONDS:-10}"
GRAPH_STARTUP_WAIT_SECONDS="${GRAPH_STARTUP_WAIT_SECONDS:-240}"
GRAPH_HEALTH_URL="${GRAPH_HEALTH_URL:-http://localhost:52104/health}"
GRAPH_CONTAINER_NAME="${GRAPH_CONTAINER_NAME:-codex-graph}"
QUEUE_MAX_RETRIES="${QUEUE_MAX_RETRIES:-3}"
QUEUE_RETRY_DELAY_SECONDS="${QUEUE_RETRY_DELAY_SECONDS:-3}"
DEMO_CONTEXT_OVERFLOW="${DEMO_CONTEXT_OVERFLOW:-false}"
SANDBOX_DEMO_ENABLED="${SANDBOX_DEMO_ENABLED:-true}"
SANDBOX_COMMAND="${SANDBOX_COMMAND:-echo hello-from-bash-sandbox}"
SANDBOX_BASE_URL="${SANDBOX_BASE_URL:-http://localhost:2000}"
SANDBOX_PROBE_URL="${SANDBOX_PROBE_URL:-${SANDBOX_BASE_URL}}"
SANDBOX_TIMEOUT_SECONDS="${SANDBOX_TIMEOUT_SECONDS:-3}"
RUN_DATE="$(date +%Y%m%d-%H%M%S)"
OUT_PATH="${OUT_PATH:-/tmp/codex-serve-example-out-${RUN_DATE}}"

INSIGHT_PAYLOAD="/tmp/codex-serve-insight-payload-${RUN_DATE}.json"
INSIGHT_RESPONSE="/tmp/codex-serve-insight-response-${RUN_DATE}.json"
GRAPH_PAYLOAD="/tmp/codex-serve-graph-payload-${RUN_DATE}.json"
GRAPH_RESPONSE="/tmp/codex-serve-graph-response-${RUN_DATE}.json"
SANDBOX_PAYLOAD="/tmp/codex-serve-sandbox-payload-${RUN_DATE}.json"
SANDBOX_RESPONSE="/tmp/codex-serve-sandbox-response-${RUN_DATE}.json"

resolve_graph_model_from_compose() {
  local compose_file="${COMPOSE_FILE:-${SCRIPT_DIR}/docker-compose.yml}"
  local line raw

  [[ -f "${compose_file}" ]] || return 0

  line="$(grep -E '^[[:space:]]*-[[:space:]]*GRAPH_MODEL=' "${compose_file}" | head -n 1 || true)"
  [[ -n "${line}" ]] || return 0

  raw="${line#*=}"
  raw="$(printf '%s' "${raw}" | sed -E 's/[[:space:]]+$//')"

  if [[ "${raw}" =~ ^\$\{GRAPH_MODEL:-([^}]*)\}$ ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
    return 0
  fi

  if [[ "${raw}" =~ ^\$\{GRAPH_MODEL\}$ ]]; then
    return 0
  fi

  printf '%s' "${raw}"
}

wait_for_graph_health() {
  local started_at elapsed

  started_at="$(date +%s)"
  while true; do
    if curl -sS -f "${GRAPH_HEALTH_URL}" >/dev/null 2>&1; then
      return 0
    fi

    elapsed=$(( $(date +%s) - started_at ))
    if [[ "${elapsed}" -ge "${GRAPH_STARTUP_WAIT_SECONDS}" ]]; then
      return 1
    fi

    sleep 2
  done
}

if [[ -z "${GRAPH_MODEL}" ]]; then
  GRAPH_MODEL="$(resolve_graph_model_from_compose)"
fi

GRAPH_MODEL_DISPLAY="${GRAPH_MODEL:-<server-default>}"

cleanup() {
  rm -f "${INSIGHT_PAYLOAD}"
  rm -f "${INSIGHT_RESPONSE}"
  rm -f "${GRAPH_PAYLOAD}"
  rm -f "${GRAPH_RESPONSE}"
  rm -f "${SANDBOX_PAYLOAD}"
  rm -f "${SANDBOX_RESPONSE}"
}
trap cleanup EXIT

mkdir -p "${OUT_PATH}"

ensure_sandbox_bash_runtime() {
  local runtimes_response install_status install_body probe_url

  probe_url="${SANDBOX_PROBE_URL}"
  if [[ "${probe_url}" == "http://codex-sandbox:2000" ]] || [[ "${probe_url}" == "http://sandbox:2000" ]]; then
    probe_url="http://localhost:2000"
  fi

  if ! runtimes_response="$(curl -sS -f "${probe_url}/api/v2/runtimes" 2>/dev/null)"; then
    echo "warning: unable to query ${probe_url}/api/v2/runtimes; skipping runtime preflight"
    return 0
  fi

  if grep -q '"language":"bash"' <<<"${runtimes_response}"; then
    echo "sandbox runtime preflight: bash is already installed"
    return 0
  fi

  echo "sandbox runtime preflight: installing bash runtime via ${probe_url}/api/v2/packages"
  install_body="$(mktemp)"
  install_status="$(curl -sS -o "${install_body}" -w "%{http_code}" -X POST "${probe_url}/api/v2/packages" \
    -H "Content-Type: application/json" \
    --data-binary '{"language":"bash","version":"*"}' || true)"

  if [[ "${install_status}" == "200" ]]; then
    echo "sandbox runtime preflight: bash runtime installed"
    rm -f "${install_body}"
    return 0
  fi

  if [[ "${install_status}" == "500" ]] && grep -qi "Already installed" "${install_body}"; then
    echo "sandbox runtime preflight: bash runtime already installed"
    rm -f "${install_body}"
    return 0
  fi

  echo "warning: failed to install bash runtime (HTTP ${install_status})"
  cat "${install_body}" || true
  rm -f "${install_body}"
}

echo "Testing POST ${BASE_URL}/agent/run with sessionId=${SESSION_ID}"
echo "Expect NDJSON stream with: session/stdout|stderr/exit"
echo "demoContextOverflow=${DEMO_CONTEXT_OVERFLOW}"

curl -N -sS -X POST "${BASE_URL}/agent/run" \
  -H "Content-Type: application/json" \
  --data-binary @- <<EOF
{
  "agent": "codex",
  "args": ["--model", "auto"],
  "stdin": "Summarize attached files in one sentence.",
  "sessionId": "${SESSION_ID}",
  "contextFiles": [
    {
      "path": "hello.c",
      "content": "#include <stdio.h>\\nint main(){printf(\"Hello\\\\n\");return 0;}"
    },
    {
      "path": "notes.txt",
      "base64Content": "SGVsbG8gZnJvbSBiYXNlNjQu"
    }
  ]
}
EOF

if [[ "${TEAM_DEMO_ENABLED}" =~ ^(1|true|yes|on)$ ]]; then
  echo
  echo "Testing POST ${BASE_URL}/agent/run in team mode with sessionId=${TEAM_SESSION_ID}"
  echo "teamAgent=${TEAM_AGENT}"

  curl -N -sS -X POST "${BASE_URL}/agent/run" \
    -H "Content-Type: application/json" \
    --data-binary @- <<EOF
{
  "agent": "${TEAM_AGENT}",
  "args": ["--model", "auto"],
  "stdin": "Analyze trade-offs for introducing strict static typing in a large legacy codebase and provide a practical phased rollout plan.",
  "sessionId": "${TEAM_SESSION_ID}"
}
EOF
fi

if [[ "${DEMO_CONTEXT_OVERFLOW}" =~ ^(1|true|yes|on)$ ]]; then
  echo
  echo "Testing optional /agent/run context-overflow auto-compress demo"

  OVERFLOW_PAYLOAD="/tmp/codex-serve-overflow-payload-${RUN_DATE}.json"
  OVERFLOW_RESPONSE="/tmp/codex-serve-overflow-response-${RUN_DATE}.ndjson"

  python3 - "${OVERFLOW_PAYLOAD}" "${RUN_DATE}" <<'PY'
import json
import sys

payload_path = sys.argv[1]
run_date = sys.argv[2]
long_prompt = "HISTORY-BLOCK-" * 120

payload = {
  "agent": "bash",
  "args": [
    "-lc",
    "INPUT=\"$(cat)\"; "
    "if printf '%s' \"$INPUT\" | grep -q 'message history compressed automatically by codex.serve'; then "
    "printf 'compressed-retry-ok'; "
    "else "
    "printf 'maximum context length exceeded\n' >&2; exit 1; "
    "fi"
  ],
  "stdin": long_prompt,
  "sessionId": f"demo-overflow-{run_date}"
}

with open(payload_path, "w", encoding="utf-8") as f:
  json.dump(payload, f)
PY

  curl -N -sS -X POST "${BASE_URL}/agent/run" \
    -H "Content-Type: application/json" \
    --data-binary "@${OVERFLOW_PAYLOAD}" \
    -o "${OVERFLOW_RESPONSE}"

  cat "${OVERFLOW_RESPONSE}"
  rm -f "${OVERFLOW_PAYLOAD}" "${OVERFLOW_RESPONSE}"
fi

echo
echo "Testing POST ${BASE_URL}/insight/run"
echo "repoDirectory=${REPO_PATH}"
echo "dryRun=${DRY_RUN}"
echo "litellmSslVerify=${LITELLM_SSL_VERIFY}"
echo "litellmCaBundle=${LITELLM_CA_BUNDLE}"
echo "outPath=${OUT_PATH}"
echo "payloadFile=${INSIGHT_PAYLOAD}"

python3 - "${REPO_PATH}" "${DRY_RUN}" "${OUT_PATH}" "${LITELLM_SSL_VERIFY}" "${LITELLM_CA_BUNDLE}" > "${INSIGHT_PAYLOAD}" <<'PY'
import base64
import json
import os
import sys

repo_path = os.path.abspath(sys.argv[1])
dry_run = sys.argv[2].strip().lower() in {"1", "true", "yes", "y", "on"}
out_path = os.path.abspath(sys.argv[3])
litellm_ssl_verify = sys.argv[4].strip().lower() in {"1", "true", "yes", "y", "on"}
litellm_ca_bundle = sys.argv[5].strip()

files = []
for root, _, names in os.walk(repo_path):
    for name in names:
        abs_path = os.path.join(root, name)
        rel_path = os.path.relpath(abs_path, repo_path).replace("\\", "/")
        with open(abs_path, "rb") as f:
            payload = base64.b64encode(f.read()).decode("ascii")
        files.append({"path": rel_path, "base64Content": payload})

body = {
    "files": files,
    "maxFilesPerModule": 40,
    "maxCharsPerFile": 10000,
    "dryRun": dry_run,
    "outPath": out_path,
    "env": {
      "LITELLM_SSL_VERIFY": "true" if litellm_ssl_verify else "false",
      "LITELLM_CA_BUNDLE": litellm_ca_bundle,
    },
}
print(json.dumps(body))
PY

insight_attempt=1
while true; do
  insight_status_code="$(curl -sS -X POST "${BASE_URL}/insight/run" \
    -H "Content-Type: application/json" \
    --data-binary "@${INSIGHT_PAYLOAD}" \
    -o "${INSIGHT_RESPONSE}" \
    -w "%{http_code}")"

  if [[ "${insight_status_code}" -ge 200 && "${insight_status_code}" -lt 300 ]]; then
    break
  fi

  if [[ "${insight_status_code}" == "503" ]] && [[ "${insight_attempt}" -lt "${QUEUE_MAX_RETRIES}" ]]; then
    echo "insight/run attempt ${insight_attempt}/${QUEUE_MAX_RETRIES} returned 503 (queue backpressure), retrying in ${QUEUE_RETRY_DELAY_SECONDS}s..."
    sleep "${QUEUE_RETRY_DELAY_SECONDS}"
    insight_attempt=$((insight_attempt + 1))
    continue
  fi

  echo "insight/run failed with HTTP ${insight_status_code}."
  cat "${INSIGHT_RESPONSE}"
  exit 1
done

cat "${INSIGHT_RESPONSE}"

python3 - "${INSIGHT_RESPONSE}" "${OUT_PATH}" <<'PY'
import json
import os
import sys

response_path = sys.argv[1]
out_path = sys.argv[2]

with open(response_path, "r", encoding="utf-8") as f:
    data = json.load(f)

files = data.get("files") or []
for item in files:
    name = item.get("path")
    content = item.get("content", "")
    if not name:
        continue
    target = os.path.join(out_path, name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as out_file:
        out_file.write(content)

print(f"materializedFiles={len(files)}")
print(f"materializedOutPath={out_path}")
print(f"serverOutputDir={data.get('outputDir', '')}")
PY

echo
echo "Testing POST ${BASE_URL}/graph/run"
echo "graphModel=${GRAPH_MODEL_DISPLAY}"
echo "graphMaxRetries=${GRAPH_MAX_RETRIES}"
echo "graphRetryDelaySeconds=${GRAPH_RETRY_DELAY_SECONDS}"
echo "graphStartupWaitSeconds=${GRAPH_STARTUP_WAIT_SECONDS}"
echo "graphHealthUrl=${GRAPH_HEALTH_URL}"
echo "graphContainerName=${GRAPH_CONTAINER_NAME}"
echo "queueMaxRetries=${QUEUE_MAX_RETRIES}"
echo "queueRetryDelaySeconds=${QUEUE_RETRY_DELAY_SECONDS}"
echo "payloadFile=${GRAPH_PAYLOAD}"

python3 - "${GRAPH_MODEL}" > "${GRAPH_PAYLOAD}" <<'PY'
import json
import os
import sys

graph_model = sys.argv[1].strip()

body = {
  "code": "from openai import OpenAI\n\nclient = OpenAI()\n\ndef summarize(text):\n    if len(text) > 200:\n        prompt = 'Summarize this long text'\n    else:\n        prompt = 'Answer this short question'\n\n    response = client.chat.completions.create(\n        model='gpt-4o-mini',\n        messages=[\n            {'role': 'system', 'content': prompt},\n            {'role': 'user', 'content': text}\n        ]\n    )\n    return response.choices[0].message.content\n",
  "file_paths": ["example/sample_workflow.py"],
  "framework_hint": "openai",
}

env = {}
for key in ("LITELLM_BASE_URL", "LITELLM_API_KEY", "LITELLM_SSL_VERIFY", "LITELLM_CA_BUNDLE"):
  value = os.environ.get(key)
  if value:
    env[key] = value

if graph_model:
  env["GRAPH_MODEL"] = graph_model

if env:
  body["env"] = env

print(json.dumps(body))
PY

graph_attempt=1
while true; do
  graph_status_code="$(curl -sS -X POST "${BASE_URL}/graph/run" \
    -H "Content-Type: application/json" \
    --data-binary "@${GRAPH_PAYLOAD}" \
    -o "${GRAPH_RESPONSE}" \
    -w "%{http_code}")"

  if [[ "${graph_status_code}" -ge 200 && "${graph_status_code}" -lt 300 ]]; then
    break
  fi

  if [[ "${graph_status_code}" == "503" ]] && [[ "${graph_attempt}" -lt "${GRAPH_MAX_RETRIES}" ]]; then
    echo "graph/run attempt ${graph_attempt}/${GRAPH_MAX_RETRIES} returned 503 (queue backpressure), retrying in ${QUEUE_RETRY_DELAY_SECONDS}s..."
    sleep "${QUEUE_RETRY_DELAY_SECONDS}"
    graph_attempt=$((graph_attempt + 1))
    continue
  fi

  if [[ "${graph_status_code}" == "504" ]] && \
     grep -q "Timed out waiting for codex.graph health endpoint after startup" "${GRAPH_RESPONSE}" && \
     [[ "${graph_attempt}" -lt "${GRAPH_MAX_RETRIES}" ]]; then
    echo "graph/run attempt ${graph_attempt}/${GRAPH_MAX_RETRIES} timed out during codex.graph startup."
    echo "Waiting for codex.graph health at ${GRAPH_HEALTH_URL} (up to ${GRAPH_STARTUP_WAIT_SECONDS}s) before next retry..."
    if wait_for_graph_health; then
      echo "codex.graph health endpoint is now ready; retrying graph/run immediately."
    else
      echo "codex.graph health endpoint is still not ready; continuing with retry delay ${GRAPH_RETRY_DELAY_SECONDS}s."
      sleep "${GRAPH_RETRY_DELAY_SECONDS}"
    fi
    graph_attempt=$((graph_attempt + 1))
    continue
  fi

  echo "graph/run failed with HTTP ${graph_status_code}."
  cat "${GRAPH_RESPONSE}"
  if command -v docker >/dev/null 2>&1; then
    echo
    echo "Last 80 lines from docker logs ${GRAPH_CONTAINER_NAME} (if container exists):"
    docker logs --tail 80 "${GRAPH_CONTAINER_NAME}" 2>/dev/null || true
  fi
  exit 1
done

cat "${GRAPH_RESPONSE}"

if [[ "${SANDBOX_DEMO_ENABLED}" =~ ^(1|true|yes|on)$ ]]; then
  echo
  echo "Testing POST ${BASE_URL}/sandbox/run"
  echo "sandboxCommand=${SANDBOX_COMMAND}"
  echo "sandboxBaseUrl=${SANDBOX_BASE_URL}"
  echo "sandboxProbeUrl=${SANDBOX_PROBE_URL}"
  echo "sandboxTimeoutSeconds=${SANDBOX_TIMEOUT_SECONDS} (capped to 3 for codex-sandbox runtime limits)"

  ensure_sandbox_bash_runtime

  python3 - "${SANDBOX_COMMAND}" "${SANDBOX_TIMEOUT_SECONDS}" > "${SANDBOX_PAYLOAD}" <<'PY'
import json
import sys

command = sys.argv[1]
timeout_raw = sys.argv[2]

try:
  timeout_seconds = float(timeout_raw)
except Exception:
  timeout_seconds = 3.0

if timeout_seconds <= 0:
  timeout_seconds = 3.0

if timeout_seconds > 3.0:
  timeout_seconds = 3.0

payload = {
  "command": command,
  "timeoutSeconds": timeout_seconds,
}

print(json.dumps(payload))
PY

  sandbox_status_code="$(curl -sS -X POST "${BASE_URL}/sandbox/run" \
    -H "Content-Type: application/json" \
    --data-binary "@${SANDBOX_PAYLOAD}" \
    -o "${SANDBOX_RESPONSE}" \
    -w "%{http_code}")"

  if [[ "${sandbox_status_code}" -ge 200 && "${sandbox_status_code}" -lt 300 ]]; then
    python3 - "${SANDBOX_RESPONSE}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

exit_code = data.get("exit_code")
timed_out = bool(data.get("timed_out"))
stdout_text = data.get("stdout", "")
stderr_text = data.get("stderr", "")

if exit_code == 0 and not timed_out:
    print("sandbox/run: PASS")
    stdout_one_line = (stdout_text or "").strip().replace("\n", "\\n")
    if stdout_one_line:
        print(f"sandbox/stdout: {stdout_one_line}")
    else:
        print("sandbox/stdout: <empty>")
else:
    print("sandbox/run: FAIL")
    print(json.dumps(data, ensure_ascii=False))
    sys.exit(1)
PY
  else
    echo "sandbox/run returned HTTP ${sandbox_status_code}."
    cat "${SANDBOX_RESPONSE}"
    exit 1
  fi
fi

echo
echo "Done. If successful, generated files are written to: ${OUT_PATH}"
