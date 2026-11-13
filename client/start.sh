#!/bin/sh
set -eu
cd "$(dirname "$0")"
printf 'H800 SSH username: '
IFS= read -r account
case "$account" in ''|*[!A-Za-z0-9_.-]*|-*) echo 'Invalid H800 username'; exit 1;; esac
echo 'Open http://127.0.0.1:18769/ after SSH connects. Keep this window open.'
exec ssh -N -T -o UserKnownHostsFile=known_hosts -o GlobalKnownHostsFile=/dev/null -o HostKeyAlgorithms=ssh-ed25519 -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -p 60011 -L 127.0.0.1:18769:127.0.0.1:18768 -l "$account" 36.103.247.214
