"""Every test module gets its own freshly seeded database."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

ORG = {"Cookie": "session=demo-organizer-7f2a"}
JUDGE_A = {"Cookie": "session=demo-judge-a-91bc"}      # jdg_24, Diego Herrera
JUDGE_B = {"Cookie": "session=demo-judge-b-44de"}      # jdg_26, Jonas Vogel
PARTICIPANT = {"Cookie": "session=demo-participant-2e88"}  # priya1, team tm_01
ADMIN = {"Cookie": "session=demo-admin-5c31"}


@pytest.fixture(scope="module")
def db_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "test.db"
    os.environ["SCORELINE_DB"] = str(path)
    os.environ["SCORELINE_FIXTURES"] = str(ROOT / "data" / "fixtures.json")
    os.environ["SCORELINE_DEMO_SESSIONS"] = "1"
    from scoreline.db import connect
    from scoreline.seed import seed
    conn = connect(str(path))
    seed(conn, verbose=False)
    conn.close()
    return path


@pytest.fixture(scope="module")
def client(db_file):
    from fastapi.testclient import TestClient
    from scoreline.main import app
    from scoreline.security import limiter
    limiter.hits.clear()
    # follow_redirects=False: a redirect must never be mistaken for access.
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def conn(db_file):
    from scoreline.db import connect
    c = connect(str(db_file))
    yield c
    c.close()


def csrf_for(client, headers, path):
    """Pull the CSRF token out of a rendered form."""
    import re
    html = client.get(path, headers=headers).text
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', html)
    assert m, f"no csrf token on {path}"
    return m.group(1)
