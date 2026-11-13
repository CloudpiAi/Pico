"""loader/build_zip.py: reproducible zips and pinned downloads (offline parts; the real build runs in the Docker image build)."""
import hashlib
import sys
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "loader"))
import build_zip  # noqa: E402


def test_same_entries_give_the_same_zip(tmp_path):
    entries = {"b.txt": b"two", "a/x.py": b"one", "duckdb_ext/v1/e.duckdb_extension": b"\x00" * 100}
    build_zip._write_zip(tmp_path / "1.zip", entries)
    build_zip._write_zip(tmp_path / "2.zip", dict(reversed(list(entries.items()))))
    assert (tmp_path / "1.zip").read_bytes() == (tmp_path / "2.zip").read_bytes()
    assert build_zip.sha256_b64(tmp_path / "1.zip") == build_zip.sha256_b64(tmp_path / "2.zip")


def test_checksum_mismatch_stops_the_build():
    pins = {"x": hashlib.sha256(b"good").hexdigest()}
    assert build_zip._checked("x", b"good", pins, update=False) == b"good"
    with pytest.raises(SystemExit, match="checksum mismatch"):
        build_zip._checked("x", b"tampered", pins, update=False)
    with pytest.raises(SystemExit):
        build_zip._checked("not-pinned", b"anything", pins, update=False)


def test_bundled_extensions_are_the_ones_the_lambda_loads():
    pytest.importorskip("boto3")
    import pico_finops_agent_loader

    assert build_zip.EXTENSIONS == pico_finops_agent_loader.EXTENSIONS


def test_update_pins_records_the_hash():
    pins = {}
    build_zip._checked("y", b"data", pins, update=True)
    assert pins == {"y": hashlib.sha256(b"data").hexdigest()}


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://pypi.org/pypi/duckdb/json", "ftp://example.com/x"])
def test_downloads_refuse_anything_but_https(url):
    with pytest.raises(urllib.error.URLError, match="unknown url type"):
        build_zip._fetch(url)
