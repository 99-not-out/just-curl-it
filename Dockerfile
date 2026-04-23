FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl jq less procps \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      confluent-kafka==2.13.2 \
      redis==5.0.* \
      pymongo==4.7.* \
      'psycopg[binary]==3.1.*' \
      pika==1.3.* \
      grpcio==1.66.2 \
      grpcio-reflection==1.66.2

WORKDIR /build
COPY intercept.c .
RUN gcc -O2 -Wall -shared -fPIC -o /usr/local/lib/intercept.so intercept.c -ldl

RUN mkdir -p /etc/intercept /usr/local/bin/handlers
COPY routes.conf   /etc/intercept/routes.conf
COPY backends.conf /etc/intercept/backends.conf
COPY handlers/ /usr/local/bin/handlers/
RUN chmod +x /usr/local/bin/handlers/*.sh /usr/local/bin/handlers/*.py

COPY bashrc        /root/.bashrc
COPY bash_history  /root/.bash_history
COPY motd          /etc/motd
COPY demo.sh       /usr/local/bin/demo
RUN chmod +x /usr/local/bin/demo

ENV LD_PRELOAD=/usr/local/lib/intercept.so
ENV INTERCEPT_DEBUG=0
ENV HISTFILE=/root/.bash_history
ENV HISTSIZE=1000
ENV HISTFILESIZE=2000
WORKDIR /root

CMD ["bash"]
