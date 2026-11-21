#!/usr/bin/env bash
# enc.sh e|d IN OUT  -- symmetric encryption of private intermediate files with $ART_KEY
set -euo pipefail
if [ "$1" = e ]; then openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:ART_KEY -in "$2" -out "$3"
else openssl enc -d -aes-256-cbc -pbkdf2 -pass env:ART_KEY -in "$2" -out "$3"; fi
