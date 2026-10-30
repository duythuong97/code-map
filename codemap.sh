#!/bin/sh
# Code Map: ./codemap.sh <init|validate|extract|serve|mcp|stale|kb> [options]   (./codemap.sh --help)
exec "$(cd "$(dirname "$0")" && pwd)/.venv/bin/python" -m codemap "$@"
