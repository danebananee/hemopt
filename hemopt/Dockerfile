ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEMOPT_DATA=/data \
    PATH="/opt/venv/bin:${PATH}"

# Debian rather than the usual Alpine base: numpy and highspy publish manylinux
# wheels but not musllinux ones, and compiling HiGHS on a Raspberry Pi during
# add-on install would take longer than most people are willing to wait.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-dev curl jq \
    && python3 -m venv /opt/venv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY optimizer/pyproject.toml /opt/hemopt/pyproject.toml
COPY optimizer/README.md /opt/hemopt/README.md
COPY optimizer/src /opt/hemopt/src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir /opt/hemopt

COPY hemopt/run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
