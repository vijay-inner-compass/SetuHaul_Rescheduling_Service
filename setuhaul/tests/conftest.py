import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest, database, seed

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.sqlite3"); seed.make_db(path); return path

@pytest.fixture
def c(db):
    return database.connect(db)
