#!/bin/bash

# Exit on error
set -e

# Change to the directory where the script is located
cd "$(dirname "$0")"

echo "Building craftslab/codex-serve:latest Docker image..."
docker build -t craftslab/codex-serve:latest .
