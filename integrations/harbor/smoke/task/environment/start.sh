#!/bin/sh
set -eu
python3 /opt/mock_endpoint.py >/tmp/mock-endpoint.log 2>&1 &
exec "$@"
