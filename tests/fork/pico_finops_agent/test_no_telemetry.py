"""Pico sends no telemetry, and keeps libraries from doing it: importing pico turns off ONNX Runtime's (Microsoft)
telemetry and LiteLLM's model-price download, unless the user set those variables themselves."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PRINT = "import os, pico; print(os.environ.get('ORT_DISABLE_TELEMETRY'), os.environ.get('LITELLM_LOCAL_MODEL_COST_MAP'))"


def _run(env_extra):
    env = {k: v for k, v in os.environ.items() if k not in ("ORT_DISABLE_TELEMETRY", "LITELLM_LOCAL_MODEL_COST_MAP")}
    env.update(env_extra)
    env["PYTHONPATH"] = str(REPO_ROOT)
    out = subprocess.run([sys.executable, "-c", PRINT], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.split()[-2:]


def test_importing_pico_turns_library_telemetry_off():
    assert _run({}) == ["1", "true"]


@pytest.mark.parametrize("value", ["0", "1"])
def test_a_users_own_setting_is_kept(value):
    assert _run({"ORT_DISABLE_TELEMETRY": value})[0] == value


def test_the_image_turns_it_off_too():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV ORT_DISABLE_TELEMETRY=1" in dockerfile
