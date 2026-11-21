"""Tests never depend on router/.env: whatever mode the live router is in, tests start from
the safe defaults (environment variables win over the .env file)."""

import os

os.environ["SEND_ENABLED"] = "false"
os.environ["TEST_MODE"] = "false"
