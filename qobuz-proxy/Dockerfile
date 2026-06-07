# Home Assistant add-on image for qobuz-proxy.
#
# Thin wrapper over the standalone qobuz-proxy application image: it only adds
# `jq` and a launcher that maps the Home Assistant options (/data/options.json)
# to the environment variables the app already understands.
ARG BUILD_FROM=ghcr.io/felixmusil/qobuz-proxy:latest
FROM ${BUILD_FROM}

# Need root to write /data, bind host-network ports, and install packages.
USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY run.sh /run.sh
RUN chmod a+x /run.sh

# Conventional Home Assistant add-on labels (Supervisor reads real metadata from
# config.yaml; these are informational).
LABEL io.hass.type="addon"

CMD [ "/run.sh" ]
