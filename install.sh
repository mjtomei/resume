#!/bin/sh
set -eu
tmux_resume_source=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$tmux_resume_source/tmux_resume.py" install "$@"
