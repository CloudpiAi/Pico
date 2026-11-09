"""Build the pico-finops-agent-loader Lambda package (python3.12, x86_64) as two reproducible zips.

  <out>/duckdb-layer.zip   layer:    python/duckdb/... (from the pinned DuckDB wheel)
  <out>/loader.zip         function: pico_finops_agent_loader.py + duckdb_ext/v<ver>/linux_amd64/{httpfs,aws,avro,iceberg}

Reproducible: files are added in sorted order with a fixed timestamp and mode, so the same inputs give
the same SHA256 (ensure_lambda updates the function only when that SHA changes). Every download is
checked against loader/pins.json; `--update-pins` records new hashes (review the diff before committing).
Each zip stays under the 50 MB direct-upload limit (DuckDB in a layer: one zip was 51.39 MB).
Run on Linux (a build host or the Docker `zips` stage).

Usage: python loader/build_zip.py <out_dir> [--update-pins]
"""
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PINS = os.path.join(HERE, "pins.json")
EXTENSIONS = ("httpfs", "aws", "avro", "iceberg")  # iceberg needs avro; Lambda cannot auto-install it
PLATFORM = "linux_amd64"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
LIMIT_ZIPPED_MB = 50
UA = {"User-Agent": "pico-finops-agent-loader-build/1.0"}


def sha256_b64(path):
    """Base64 SHA256 of a file, the same format Lambda reports as CodeSha256."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode()


def _fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
        return r.read()


def _checked(name, data, pins, update):
    digest = hashlib.sha256(data).hexdigest()
    if update:
        pins[name] = digest
    elif pins.get(name) != digest:
        raise SystemExit(f"build_zip: checksum mismatch for {name}: got {digest}, pinned {pins.get(name)}")
    return data


def _wheel_url(version):
    meta = json.loads(_fetch(f"https://pypi.org/pypi/duckdb/{version}/json"))
    for f in meta["urls"]:
        if "cp312-cp312-manylinux" in f["filename"] and f["filename"].endswith("x86_64.whl"):
            return f["filename"], f["url"]
    raise SystemExit(f"build_zip: no cp312 manylinux x86_64 wheel for duckdb {version}")


def _write_zip(dest, entries):
    """entries: {arcname: bytes}. Sorted, fixed time and mode: same bytes in, same zip out."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for arc in sorted(entries):
            info = zipfile.ZipInfo(arc, date_time=FIXED_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, entries[arc])
    return sum(len(v) for v in entries.values())


def build(out_dir, update_pins=False):
    with open(PINS, encoding="utf-8") as fh:
        pins = json.load(fh)
    version = pins["duckdb_version"]
    os.makedirs(out_dir, exist_ok=True)

    wheel_name, wheel_url = _wheel_url(version)
    wheel = _checked(wheel_name, _fetch(wheel_url), pins, update_pins)
    layer = {}
    with zipfile.ZipFile(io.BytesIO(wheel)) as w:
        for n in w.namelist():
            if n.endswith("/") or (".dist-info/" in n and not n.endswith(("METADATA", "LICENSE"))):
                continue
            layer[f"python/{n}"] = w.read(n)

    func = {}
    for ext in EXTENSIONS:
        name = f"{ext}.duckdb_extension.gz@v{version}/{PLATFORM}"
        raw = _checked(name, _fetch(f"https://extensions.duckdb.org/v{version}/{PLATFORM}/{ext}.duckdb_extension.gz"),
                       pins, update_pins)
        func[f"duckdb_ext/v{version}/{PLATFORM}/{ext}.duckdb_extension"] = gzip.decompress(raw)
    with open(os.path.join(HERE, "pico_finops_agent_loader.py"), "rb") as fh:
        func["pico_finops_agent_loader.py"] = fh.read()

    layer_zip, func_zip = os.path.join(out_dir, "duckdb-layer.zip"), os.path.join(out_dir, "loader.zip")
    layer_raw, func_raw = _write_zip(layer_zip, layer), _write_zip(func_zip, func)
    if update_pins:
        with open(PINS, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(pins, fh, indent=2, sort_keys=True)
            fh.write("\n")
    mb = lambda n: round(n / 2**20, 2)  # noqa: E731
    rec = {"duckdb": version, "layer_mb": mb(os.path.getsize(layer_zip)), "function_mb": mb(os.path.getsize(func_zip)),
           "unzipped_mb": mb(layer_raw + func_raw), "layer_sha256": sha256_b64(layer_zip),
           "function_sha256": sha256_b64(func_zip)}
    rec["ok"] = rec["layer_mb"] < LIMIT_ZIPPED_MB and rec["function_mb"] < LIMIT_ZIPPED_MB
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--update-pins", action="store_true")
    a = ap.parse_args(argv)
    rec = build(a.out_dir, a.update_pins)
    print(json.dumps(rec))
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
