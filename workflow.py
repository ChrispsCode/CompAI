#!/usr/bin/env python3
"""Workflow tools: check / scaffold / list / run. See `python3 workflow.py -h`."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "nuke_plugin"))
from compai import cli

sys.exit(cli.main())
