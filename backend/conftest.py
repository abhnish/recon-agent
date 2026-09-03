"""
conftest.py
───────────
Pytest configuration for the backend test suite.

Sets sys.path so that ``from app.services import ...`` works when pytest is
invoked from the ``backend/`` directory.
"""

import sys
from pathlib import Path

# Make ``backend/`` the root for absolute imports within the app package
sys.path.insert(0, str(Path(__file__).parent))
