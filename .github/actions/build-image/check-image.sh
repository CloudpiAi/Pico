#!/bin/sh
# Checks a built Pico FinOps Agent image. Usage: sh .github/actions/build-image/check-image.sh <image> [<repo-root>]
# The same numbered checks as the maintainers' private image check, with the public names; 11 is public-only.
# When the Dockerfile gains the schedule plugin and precompiled code, add checks 2b and 2c here too.
# Any failed check exits 1.
set -u
IMG=${1:?usage: check-image.sh <image> [<repo-root>]}
HERE=${2:-.}
fails=0
ok()  { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; fails=$((fails + 1)); }
run() { docker run --rm --network none --entrypoint "$@"; }

# 1. Nothing of the CloudPi platform: no plugins/cloudpi folder anywhere, no code importing it,
#    no pico-cloudpi command or entry point, no platform profile.
if run sh "$IMG" -c '
  ! find /app /home/pico -path "*/plugins/cloudpi" | grep -q . &&
  test ! -e /app/.venv/bin/pico-cloudpi && test ! -e /app/deploy/agent.yml &&
  ! grep -rqsE "^[[:space:]]*(from|import) plugins[.]cloudpi" --include="*.py" /app &&
  ! grep -rqs "plugins.cloudpi" /app/.venv/lib/python3.13/site-packages/*.dist-info/entry_points.txt'; then
  ok "no CloudPi platform code"
else bad "CloudPi platform code is in the image"; fi

# 2. The focus-data plugin is found and its command runs, from the agent's working folder without --config.
if run sh "$IMG" -c 'cd /pico-workspace && pico focus-data status --help' >/dev/null 2>&1; then
  ok "pico focus-data is found without --config"
else bad "pico focus-data doesn't run from /pico-workspace"; fi


# 3. The `pico-finops-agent` command (serve | check | health), the one-process agent, its scheduler and health check, and
#    both Lambda zips are there (and the old two-program launchers are not).
if run sh "$IMG" -c 'python -c "import pico_finops_agent.agent, pico_finops_agent.scheduler, pico_finops_agent.health, apscheduler, boto3" &&
  pico-finops-agent --help | grep -q "serve" && pico-finops-agent --help | grep -q "check" && pico-finops-agent --help | grep -q "health" &&
  test ! -e /app/pico_finops_agent/api.py && test ! -e /app/pico_finops_agent/slack.py && test ! -e /usr/local/bin/pico-finops-agent-run &&
  test -s /opt/pico-finops-agent-loader/duckdb-layer.zip && test -s /opt/pico-finops-agent-loader/loader.zip'; then
  ok "agent code and Lambda zips present"
else bad "agent code or Lambda zips missing"; fi

# 4. Started with no settings, it refuses to start and names the first missing one (FOCUS_REGION).
out=$(docker run --rm --network none "$IMG" 2>&1); rc=$?
if [ "$rc" -eq 1 ] && echo "$out" | grep -q "FOCUS_REGION is not set"; then
  ok "empty settings: exits 1 naming FOCUS_REGION"
else bad "empty settings: exit $rc, output: $(echo "$out" | tail -2)"; fi

# 5. The entrypoint checks .env and runs `pico-finops-agent`, whose default command is `serve`
#    (the one-process agent); the health check is `pico-finops-agent health`; no port is declared.
if [ "$(docker image inspect "$IMG" --format '{{json .Config.Entrypoint}}')" = '["pico-finops-agent-entrypoint","pico-finops-agent"]' ] &&
   [ "$(docker image inspect "$IMG" --format '{{json .Config.Cmd}}')" = '["serve","--config","/home/pico/.pico/conf/agent.yml"]' ] &&
   [ "$(docker image inspect "$IMG" --format '{{json .Config.Healthcheck.Test}}')" = '["CMD","pico-finops-agent","health"]' ] &&
   [ "$(docker image inspect "$IMG" --format '{{json .Config.ExposedPorts}}')" = "null" ]; then
  ok "entrypoint + 'pico-finops-agent serve' by default, 'pico-finops-agent health' check, no port"
else bad "the image's entrypoint, command, health check or ports are not the agent's"; fi

# 6. docker-compose.yml, as Docker reads it (docker compose config), publishes no port and uses host networking.
cfg_dir=$(mktemp -d); trap 'rm -rf "$cfg_dir"' EXIT
cp "$HERE/docker-compose.yml" "$cfg_dir/"; : > "$cfg_dir/.env"
compose_json=$(AGENT_IMAGE_TAG=check docker compose -f "$cfg_dir/docker-compose.yml" config --format json 2>/dev/null | tr -d ' \n')
if [ -n "$compose_json" ] && echo "$compose_json" | grep -q '"network_mode":"host"' &&
   ! echo "$compose_json" | grep -q '"ports"'; then
  ok "compose: no published port, host networking"
else bad "compose publishes a port, isn't host networking, or doesn't parse"; fi

# 7. No .env file, private key, Slack token, AWS key or provider key in the image. Third-party packages are skipped
#    (their public CA bundles are .pem files), and so are .md docs (placeholder examples like BEGIN PRIVATE KEY ...).
if run sh "$IMG" -c '
  ! find /app /home/pico /opt /usr/local/bin -path /app/.venv -prune -o \( -name ".env" -o -name ".env.*" ! -name ".env.example" -o -name "*.pem" -o -name "*.key" \) -print | grep -q . &&
  ! grep -rIlE "xox[abp]-[0-9]{6,}|xapp-[0-9]-[A-Z0-9]{8,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{40,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|aws_secret_access_key *= *[A-Za-z0-9/+]{40}" \
    --exclude-dir=.venv --exclude="*.md" /app /home/pico /opt /usr/local/bin 2>/dev/null | grep -q .'; then
  ok "no .env, keys or tokens"
else bad "a .env file, a key or a token-like string is in the image"; fi

# 8. The DuckDB extensions the local copy needs load with no network, on a read-only filesystem (as in compose).
if docker run --rm --network none --read-only --entrypoint python "$IMG" -c 'import duckdb; c = duckdb.connect()
for e in ("httpfs", "aws", "iceberg", "avro"): c.sql("INSTALL " + e); c.sql("LOAD " + e)' >/dev/null 2>&1; then
  ok "DuckDB extensions load offline, read-only"
else bad "DuckDB extensions need a download or a writable home folder"; fi

# 9. Docker's health check command: healthy only on a fresh heartbeat that says healthy (the agent writes it every 5 s).
if docker run --rm --network none --read-only --tmpfs /tmp --entrypoint python "$IMG" -c '
import json, os, subprocess, sys, time
path = "/tmp/pico-finops-agent-health.json"
def check(age, status):
    with open(path, "w") as fh:
        json.dump({"ts": time.time() - age, "status": status, "reason": ""}, fh)
    env = {**os.environ, "PICO_FINOPS_AGENT_HEALTH_FILE": path}
    return subprocess.run(["pico-finops-agent", "health"], env=env, capture_output=True).returncode
assert check(0, "healthy") == 0, "fresh healthy"
assert check(120, "healthy") == 1, "stale"
assert check(0, "unhealthy") == 1, "unhealthy"
' >/dev/null 2>&1; then
  ok "health check: fresh healthy passes, stale or unhealthy fails"
else bad "the heartbeat health check doesn't behave"; fi

# 10. Runs as the pico user, not root.
if [ "$(docker run --rm --network none --entrypoint id "$IMG" -u)" = "10001" ]; then ok "runs as uid 10001"
else bad "does not run as uid 10001"; fi

# 11. No CloudPi text in the agent's own files. Allowed: NOTICE's copyright line, this repository's URL and the
#     published image's name (ghcr.io/cloudpiai/pico-finops-agent).
hits=$(run sh "$IMG" -c 'grep -rIin "cloudpi" --exclude-dir=.venv --exclude=NOTICE /app /home/pico /opt /usr/local/bin 2>/dev/null' | grep -vi -e "github.com/CloudpiAi/Pico" -e "ghcr.io/cloudpiai/pico-finops-agent")
if [ -z "$hits" ]; then
  ok "no CloudPi text outside NOTICE"
else bad "CloudPi text is in the image: $(echo "$hits" | head -3 | cut -c1-160 | tr '
' ' ')"; fi

echo "---"
[ "$fails" -eq 0 ] && echo "All checks passed." && exit 0
echo "$fails check(s) failed." && exit 1
