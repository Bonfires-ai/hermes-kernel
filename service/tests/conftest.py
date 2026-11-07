"""Shared values: the bonfire the tests speak to, and the one database the Mongo tests share."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from pymongo import MongoClient

BONFIRE = "65f0c0ffee0000000000abcd"


@pytest.fixture(scope="session")
def shared_database() -> Iterator[str]:
    """One database for every test that empties it after itself (``KT_TEST_MONGO_URI``): mongod keeps a file open for
    every collection and index until the checkpoint after its drop, so a database per test runs a container out of
    files."""
    name = f"kt_test_{uuid.uuid4().hex}"
    yield name
    uri = os.environ.get("KT_TEST_MONGO_URI")
    if uri:
        with MongoClient(uri) as client:
            client.drop_database(name)
