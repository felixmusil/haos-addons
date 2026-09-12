# Home Assistant add-on image for Transmission behind NordVPN (OpenVPN).
#
# Thin wrapper over haugene/transmission-openvpn: it adds nginx (the ingress shim
# that injects the RPC credentials), the "Add from AudioBookBay" helper and a
# launcher that maps the Home Assistant options (/data/options.json) to the
# environment variables the image already understands. The image ships bash,
# jq, python3, openssl and dumb-init; only nginx is missing.
ARG BUILD_FROM=haugene/transmission-openvpn:5.5.2
FROM ${BUILD_FROM}

# Retries: Ubuntu apt over IPv4 HTTP is flaky from this network.
RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY rootfs/ /
RUN chmod a+x /run.sh

# Conventional Home Assistant add-on labels (Supervisor reads real metadata from
# config.yaml; these are informational).
LABEL io.hass.type="addon"

CMD [ "/run.sh" ]
