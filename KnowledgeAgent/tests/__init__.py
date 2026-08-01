"""Test suite for KnowledgeAgent.

Ensures the package is importable regardless of the working directory the test
runner is launched from.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
