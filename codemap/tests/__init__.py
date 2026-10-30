"""Tests run against the demo config whatever the local .env says.

Set before the app reads its settings (values already in the environment win over .env).
"""
import os

from codemap import PROJECT_ROOT

os.environ["CODE_MAP_CONFIG"] = str(PROJECT_ROOT / "examples" / "demo-config.json")
os.environ["CODE_MAP_BACKEND_LOG_LEVEL"] = "WARNING"
os.environ.setdefault("CODE_MAP_KNOWLEDGE_TOKEN", "test-token")
