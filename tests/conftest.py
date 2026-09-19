"""Test configuration: isolate tests from the real lifeos.db.

pytest loads conftest.py before importing any test module, so setting the
LIFEOS_DB_PATH env var here guarantees the app under test writes to a
throwaway SQLite file instead of the production database.
"""
import os
import tempfile

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="lifeos_test_"), "test.db")
os.environ["LIFEOS_DB_PATH"] = _TMP_DB