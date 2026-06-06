"""Backwards-compatibility shim.

All imports from the old root-level config.py continue to work::

    from config import get_ollama_config, get_sqlserver_uri, load_env_file

Real implementation lives in src/sql_agent/config.py.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from sql_agent.config import (   # noqa: F401  re-export
    Settings,
    load_env_file,
    get_ollama_config,
    get_sqlserver_uri,
    print_config_status,
)
