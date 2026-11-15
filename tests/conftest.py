"""Stable dungeon fixtures for tests which send scripted input sequences."""
import os

# The in-memory test transport cannot sendfile(); aiohttp reads this flag when
# web_fileresponse is first imported, so set it before any test module loads.
os.environ.setdefault('AIOHTTP_NOSENDFILE', '1')

import pytest


@pytest.fixture(autouse=True)
def scripted_attempt_seed(monkeypatch):
    # Random encounters can add --More-- prompts and consume a script's save or
    # quit keys. Hold the dungeon fixed, not owner/player credentials. Engine
    # replay tests still exercise their explicitly chosen seeds independently.
    monkeypatch.setattr('nethack_run.attempt.secrets.token_hex', lambda size: '00' * size)
