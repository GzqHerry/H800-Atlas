#!/bin/sh
set -eu
curl --noproxy '*' --silent --fail --max-time 3 http://127.0.0.1:18768/ >/dev/null
echo 'H800 Atlas ready.'
exec sleep infinity
