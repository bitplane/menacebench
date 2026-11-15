FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build
# Freeze build packages as well as the base image.
RUN printf '%s\n' 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261101T000000Z bookworm main' 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261101T000000Z bookworm-security main' > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends gcc make bison flex libncurses-dev git curl ca-certificates groff bsdextrautils \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir /runtime-libs && cp -L /lib/*-linux-gnu/libncurses.so.6 /lib/*-linux-gnu/libtinfo.so.6 /runtime-libs/
WORKDIR /opt/nethack-run
COPY scripts scripts
COPY patches patches
COPY config config
RUN python scripts/setup.py
COPY requirements.lock requirements.lock
RUN python -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock
COPY src src
COPY PLAYER.md DESIGN.md LICENSE-NetHack nethack-run pyproject.toml ./
COPY tests tests
RUN NETHACK_TEST_LIVE=1 .venv/bin/python -m pytest -q

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
WORKDIR /opt/nethack-run
COPY --from=build /opt/nethack-run/.venv .venv
COPY --from=build --chown=1000:1000 /opt/nethack-run/build/install build/install
COPY --from=build /opt/nethack-run/build/build.json build/build.json
COPY --from=build /runtime-libs /opt/nethack-run/lib
ENV LD_LIBRARY_PATH=/opt/nethack-run/lib
COPY src src
COPY config config
COPY PLAYER.md DESIGN.md LICENSE-NetHack nethack-run pyproject.toml ./
RUN mkdir /runs && chown 1000:1000 /runs
USER 1000:1000
ENV PYTHONDONTWRITEBYTECODE=1
RUN PYTHONPATH=src .venv/bin/python -c 'import tempfile; from pathlib import Path; from nethack_run.engine import Game, clean_directory; t=tempfile.TemporaryDirectory(); p=Path(t.name)/"game"; clean_directory(p); g=Game(p,42); assert g.frame and g.boundary==1; g.close(); t.cleanup()'
EXPOSE 8000
ENTRYPOINT ["./nethack-run"]
CMD ["serve", "--host", "0.0.0.0", "--state", "/runs"]
