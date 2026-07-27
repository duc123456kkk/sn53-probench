#!/usr/bin/env bash
# Wrapper tuong thich nguoc — moi thu nam o box/serve.sh (env-driven).
exec env TP=2 bash "$(dirname "$0")/serve.sh"
