# Multi-architecture base (amd64, aarch64); Alpine 3.21 carries Python 3.12, which the tests use.
FROM ghcr.io/home-assistant/base:3.21

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

# The server is the Python standard library; nothing is pip-installed.
# tzdata: local days are Home Assistant's time zone (zoneinfo reads /usr/share/zoneinfo)
RUN apk add --no-cache python3 ca-certificates tzdata \
 && python3 -c "import http.server, ssl, urllib.request, zoneinfo; zoneinfo.ZoneInfo('America/Chicago'); print('python ok')"

WORKDIR /opt/loops
COPY server.py checks.py overrides.py page.py hass_ws.py controller.py collector.py network.py ./

COPY panel.html ./

RUN python3 -m py_compile server.py checks.py overrides.py page.py hass_ws.py controller.py collector.py network.py

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV PYTHONUNBUFFERED=1

# The Supervisor's watchdog follows Docker's health state.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
  CMD ["/bin/sh", "-c", "curl -fsS -o /dev/null http://127.0.0.1:8095/healthz || exit 1"]

CMD ["/run.sh"]

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=dev
LABEL \
    io.hass.name="Loops" \
    io.hass.description="Nightly health checks of Home Assistant, and the automations people undo by hand" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    maintainer="Lewis Heidrick" \
    org.opencontainers.image.title="Loops" \
    org.opencontainers.image.description="Nightly health checks of Home Assistant, and the automations people undo by hand" \
    org.opencontainers.image.vendor="Lewis Heidrick" \
    org.opencontainers.image.authors="Lewis Heidrick" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.url="https://github.com/heidrickla/ha-loops" \
    org.opencontainers.image.source="https://github.com/heidrickla/ha-loops" \
    org.opencontainers.image.documentation="https://github.com/heidrickla/ha-loops/blob/main/loops/DOCS.md" \
    org.opencontainers.image.version="${BUILD_VERSION}"
