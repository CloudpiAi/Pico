"""`pico focus-data <load|status>`. Called by Pico as main(argv, profile)."""
import argparse
import json
import sys

from . import loader


def main(argv, profile):
    p = argparse.ArgumentParser(prog="pico focus-data")
    sub = p.add_subparsers(dest="cmd", required=True)
    ld = sub.add_parser("load", help="refresh the S3 table (through the pico-finops-agent-loader Lambda) and the local cache")
    ld.add_argument("--force", action="store_true", help="reload the due months even if nothing new was delivered")
    sub.add_parser("status", help="AWS identity, last load, last error, months in the table")
    a = p.parse_args(argv)

    cfg = loader.Config.from_profile(profile)
    if a.cmd == "load":
        return loader.run_load(cfg, force=a.force)
    json.dump(loader.status(cfg), sys.stdout, indent=2, default=str)
    print()
    return 0
