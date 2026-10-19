#!/usr/bin/env bash
# Pull the latest code and rebuild (same as re-running install.sh). data/ is untouched.
exec "$(dirname "$0")/../install.sh"
