FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        cabextract \
        git \
        python3 \
        python3-pip \
        python3-venv \
        unzip \
        wine \
        wine32:i386 \
        winetricks \
        x11-utils \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/srim-venv \
    && /opt/srim-venv/bin/python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /opt/srim-venv/bin/python -m pip install --no-cache-dir matplotlib numpy pyyaml

COPY docker/entrypoint.sh /usr/local/bin/srim-entrypoint

RUN chmod 0755 /usr/local/bin/srim-entrypoint \
    && mkdir -p /data/home-volume /data/srim-volume /data/wine-volume /workspace/SRIM_scripts /workspace/pysrim \
    && chmod 0777 /data/home-volume /data/srim-volume /data/wine-volume

ENV PATH=/opt/srim-venv/bin:$PATH
ENTRYPOINT ["/usr/local/bin/srim-entrypoint"]
