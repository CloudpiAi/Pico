# Pico FinOps Agent image: answers AWS cost questions in Slack from the customer's own AWS FOCUS export.
# Build from the repo root: docker compose -f docker-compose.yml -f docker-compose.build.yml build
#   (or: docker build -t pico-finops-agent:local .). Released images: ghcr.io/cloudpiai/pico
# No secret or customer setting goes in the image: they come from the customer's .env (docs/CUSTOMER-SETUP-GUIDE.md).
# .dockerignore is an allow-list: only the files copied below are sent to the build.
ARG PYTHON_IMAGE=python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

# 1. The loader Lambda's two zips. The DuckDB wheel and extensions are checked against loader/pins.json.
FROM ${PYTHON_IMAGE} AS zips
WORKDIR /src
COPY ./loader ./loader
# Also take the four checked DuckDB extensions back out of loader.zip (duckdb_ext/v<ver>/linux_amd64/*), for the
# runtime stage below: the local copy then uses the same files as the Lambda.
RUN python loader/build_zip.py /out \
 && test -s /out/duckdb-layer.zip && test -s /out/loader.zip \
 && python -c "import zipfile; z = zipfile.ZipFile('/out/loader.zip'); \
      z.extractall('/out/ext', [n for n in z.namelist() if n.startswith('duckdb_ext/')])" \
 && ls /out/ext/duckdb_ext/v*/linux_amd64/iceberg.duckdb_extension

# 2. The Python environment: the Pico framework, the agent's extra, the focus-data and the schedule plugins.
FROM ${PYTHON_IMAGE} AS build
WORKDIR /app
# The same uv that wrote uv.lock; an older one may reject or rewrite it.
RUN pip install --no-cache-dir uv==0.9.12
COPY ./pyproject.toml ./uv.lock ./README.md ./LICENSE ./NOTICE ./
# Packages in uv.lock the agent never imports, left out of the image (~165 MB):
#   sympy + mpmath (~80 MB): required by onnxruntime, but only its optional tools (quantization, transformers) use them
#   onnx (~79 MB): required by fastembed, which runs models with onnxruntime and does not import onnx
ARG SKIP_PACKAGES="--no-install-package sympy --no-install-package mpmath --no-install-package onnx"
RUN uv sync --frozen --no-dev --extra pico-finops-agent --no-install-project $SKIP_PACKAGES
COPY ./pico ./pico
COPY ./pico_finops_agent ./pico_finops_agent
COPY ./plugins/focus ./plugins/focus
COPY ./plugins/schedule ./plugins/schedule
COPY ./loader/pins.json /tmp/pins.json
# The plugins' packages (boto3, duckdb) come from uv.lock, not resolved again; the lock's DuckDB must be the one
# the Lambda and the extensions are pinned to (loader/pins.json), or the local copy can't load them.
RUN uv sync --frozen --no-dev --extra pico-finops-agent $SKIP_PACKAGES \
 && uv pip install --python /app/.venv/bin/python --no-deps ./plugins/focus ./plugins/schedule \
 && /app/.venv/bin/python -c "import json, boto3, duckdb; want = json.load(open('/tmp/pins.json'))['duckdb_version']; \
      assert duckdb.__version__ == want, (duckdb.__version__, want)" \
 && rm -rf ./plugins/focus/build ./plugins/schedule/build /tmp/pins.json
# Trim the third-party packages before they are precompiled below (so no .pyc is made for what is removed). Only
# /app reaches the runtime stage, so removing here is enough to keep the bytes out of the image:
#   - their test suites (pandas/tests alone is ~30-40 MB), never anything of Pico's own (/app/pico, /app/plugins)
#   - type stubs (*.pyi), Cython headers (*.pxd) and pyarrow's C++ headers: only a compiler or a type checker reads them
#   - debug symbols in native libraries (strip --strip-unneeded); a file strip cannot handle is left as it is
# binutils (strip) is installed in this stage only; the runtime stage never gets it.
RUN apt-get update && apt-get install -y --no-install-recommends binutils && rm -rf /var/lib/apt/lists/* \
 && SP=$(/app/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])') \
 && du -sm "$SP" \
 && find "$SP" -mindepth 2 -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + \
 && find "$SP" -type f \( -name '*.pyi' -o -name '*.pxd' \) -delete \
 && rm -rf "$SP/pyarrow/include" \
 && { find "$SP" -type f -name '*.so*' -exec strip --strip-unneeded {} + 2>/dev/null || true; } \
 && du -sm "$SP"
# Precompile everything: the container's disk is read-only, so Python can't write a .pyc cache at run time and every
# `pico` command (each Slack task, each scheduled question) would recompile ~3,200 files (~5 s). Hash-checked .pyc stay
# valid however the copy below sets file times. Pico's own code must compile, so a syntax error there fails the
# build; some third-party packages ship test files that don't compile, so only those failures are tolerated.
RUN /app/.venv/bin/python -m compileall -q -j 3 --invalidation-mode checked-hash /app/pico /app/pico_finops_agent \
 && { /app/.venv/bin/python -m compileall -q -j 3 --invalidation-mode checked-hash \
      "$(/app/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" >/dev/null || true; }

# 3. What runs.
FROM ${PYTHON_IMAGE}
# /data is the volume (cache, state, logs); /pico-workspace is the agent's working folder.
# The base image's pip is never used at run time (the container is read-only and installs nothing), so it goes.
RUN python -m pip uninstall -y -q pip \
 && useradd --create-home --uid 10001 pico \
 && mkdir -p /data /pico-workspace /home/pico/.pico/conf /opt/pico-finops-agent-loader \
 && chown -R pico:pico /data /pico-workspace /home/pico/.pico
WORKDIR /app
COPY --from=build --chown=pico:pico /app /app
COPY --from=zips /out/duckdb-layer.zip /out/loader.zip /opt/pico-finops-agent-loader/
# The same pinned, checked DuckDB extensions as the Lambda, where DuckDB looks for them: the local copy's rebuild
# then loads them without a download, and nothing is written to the read-only home folder.
COPY --from=zips --chown=pico:pico /out/ext/duckdb_ext /home/pico/.duckdb/extensions
# Pico finds this file when no --config is given, which `pico focus-data load` relies on.
COPY --chown=pico:pico ./deploy/pico-finops-agent.yml /home/pico/.pico/conf/agent.yml
# A start check, then one command with sub-commands. pico_finops_agent isn't an installed package, so the
# `pico-finops-agent` command is a small shim for `python -m pico_finops_agent.cli`.
COPY --chmod=755 ./pico_finops_agent/entrypoint.sh /usr/local/bin/pico-finops-agent-entrypoint
RUN printf '#!/bin/sh\nexec python -m pico_finops_agent.cli "$@"\n' > /usr/local/bin/pico-finops-agent \
 && chmod 755 /usr/local/bin/pico-finops-agent
USER pico
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app PYTHONUNBUFFERED=1
VOLUME /data
# No EXPOSE: the agent runs no server and opens no port; Slack is reached outbound (Socket Mode).
# Health: the agent writes a heartbeat every 5 s (scheduler alive, Slack connected or switched off); this reads it.
# The agent itself exits for a restart when Slack has been down 10 minutes or the scheduler is stuck.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD ["pico-finops-agent", "health"]
# pico-finops-agent-entrypoint checks .env, then runs `pico-finops-agent <command>`:
#   serve  the whole agent in one process (the default): the data-collection scheduler, plus Pico's Slack agent
#          when both Slack tokens are set
#   check  pre-flight, posts nothing: docker compose run --rm agent check
ENTRYPOINT ["pico-finops-agent-entrypoint", "pico-finops-agent"]
CMD ["serve", "--config", "/home/pico/.pico/conf/agent.yml"]
