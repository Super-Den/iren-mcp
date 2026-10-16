#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility launcher for the canonical repository MCP server.

The implementation lives at the repository root so the skill and the plugin
cannot silently drift apart.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from iren_mcp_server import *  # noqa: F401,F403,E402

if __name__ == "__main__":
    cli()
