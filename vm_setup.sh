#!/usr/bin/env bash
# Bootstrap an Ubuntu 22.04/24.04 VM for the pipeline and fetch the input.
# Usage: bash vm_setup.sh /mnt/data
set -euo pipefail
ROOT="${1:-/mnt/data}"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip unar p7zip-full ncompress gzip curl aria2 >/dev/null
mkdir -p "$ROOT"/{input,work,out,scratch}
cd "$ROOT"
python3 -m venv venv
./venv/bin/pip install -q -r "$(dirname "$0")/requirements.txt"
curl -fsSLO https://machina-input.joinbuilders.org/get-input.sh
bash get-input.sh --dest "$ROOT/input" --jobs 64 --aria2 || bash get-input.sh --dest "$ROOT/input" --jobs 64
