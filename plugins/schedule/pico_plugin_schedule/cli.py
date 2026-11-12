"""`pico schedule <action> ...`. Called by Pico as main(argv, profile); see task_cli for the actions."""
import os

from . import source, task_cli


def main(argv, profile):
    profile = dict(profile or {})
    profile["data_dir"] = profile.get("data_dir") or os.environ.get("FOCUS_DATA_DIR") or "/data"
    return task_cli.run(argv, profile["data_dir"], source.load(profile.get("source"), profile))
