ARG BUILD_FROM
FROM ${BUILD_FROM}

# Alpine ships numpy prebuilt, so nothing is compiled during install and the
# add-on installs in well under a minute, even on a Raspberry Pi.
RUN apk add --no-cache python3 py3-numpy tzdata jq

ENV PYTHONPATH=/opt/slingkoll/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SLINGKOLL_DATA=/data

COPY src /opt/slingkoll/src
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
