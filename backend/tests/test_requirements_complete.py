"""Regression: FastAPI's UploadFile/Form routes make the app crash at IMPORT
(RuntimeError) unless python-multipart is installed. It was once present only
by accident in the dev environment, so a clean `pip install -r requirements.txt`
(Docker, Render, CI) could not boot the API. Pin it explicitly."""

import os
import re

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _names(path):
    out = set()
    for line in open(os.path.join(BACKEND, path), encoding="utf-8"):
        m = re.match(r"^([A-Za-z0-9_.\-]+)", line.strip())
        if m and not line.lstrip().startswith("#"):
            out.add(m.group(1).lower().replace("_", "-"))
    return out


def test_python_multipart_is_a_runtime_requirement():
    assert "python-multipart" in _names("requirements.txt")
    assert "python-multipart" in _names("requirements.lock")


def test_every_runtime_requirement_is_in_the_lock():
    assert _names("requirements.txt") <= _names("requirements.lock")
