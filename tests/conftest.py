"""Stable dungeon fixtures for tests which send scripted input sequences."""
import pytest


@pytest.fixture(autouse=True)
def scripted_attempt_seed(monkeypatch):
    # Random encounters can add --More-- prompts and consume a script's save or
    # quit keys. Hold the dungeon fixed, not owner/player credentials. Engine
    # replay tests still exercise their explicitly chosen seeds independently.
    monkeypatch.setattr('nethack_run.attempt.secrets.token_hex', lambda size: '00' * size)
