#!/bin/bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SESSION_ID="demo-$(date +%s)"
REPO_PATH="${REPO_PATH:-$(pwd)}"
DRY_RUN="${DRY_RUN:-true}"

INSIGHT_PAYLOAD="$(mktemp)"

cleanup() {
  rm -f "${INSIGHT_PAYLOAD}"
}
trap cleanup EXIT

echo "Testing POST ${BASE_URL}/agent/run with sessionId=${SESSION_ID}"
echo "Expect NDJSON stream with: session/stdout|stderr/exit"

curl -N -sS -X POST "${BASE_URL}/agent/run" \
  -H "Content-Type: application/json" \
  --data-binary @- <<EOF
{
  "agent": "codex",
  "args": ["--model", "ollama-kimi-k2.5"],
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

echo
echo "Testing POST ${BASE_URL}/insight/run"
echo "repoDirectory=${REPO_PATH}"
echo "dryRun=${DRY_RUN}"

python3 - "${REPO_PATH}" "${DRY_RUN}" > "${INSIGHT_PAYLOAD}" <<'PY'
import base64
import json
import os
import sys

repo_path = os.path.abspath(sys.argv[1])
dry_run = sys.argv[2].strip().lower() in {"1", "true", "yes", "y", "on"}

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
}
print(json.dumps(body))
PY

curl -sS -X POST "${BASE_URL}/insight/run" \
  -H "Content-Type: application/json" \
  --data-binary "@${INSIGHT_PAYLOAD}"

echo
echo "Done. If successful, response includes stdout/stderr/exit_code and generated files from uploaded directory content."
