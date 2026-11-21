#!/usr/bin/env bash
# Prepare a GitHub-hosted Ubuntu runner: free disk, install tools, fetch the manifest.
set -euo pipefail
sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /opt/hostedtoolcache/CodeQL /usr/local/share/boost || true
sudo apt-get update -qq >/dev/null
sudo apt-get install -y -qq ncompress unar p7zip-full >/dev/null
python3 -m pip install -q -r requirements.txt
sudo mkdir -p /work && sudo chown "$USER" /work
curl -fsSL --retry 5 -o /work/manifest.tsv https://machina-input.joinbuilders.org/manifest.tsv
df -h / /mnt | tail -2
