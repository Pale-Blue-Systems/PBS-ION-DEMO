# PBS ION demo in a container: the same ./run_demo.sh as on a host.
#
#   docker build -t pbs-ion-demo .
#   docker run --rm --shm-size=256m -v "$PWD/docker-results:/demo/results" pbs-ion-demo
#
# pkgconf provides pkg.m4, which autoreconf needs for ION's configure.ac
# (PKG_CHECK_MODULES); no other listed package pulls it in.
#
# ION is built inside the container at the commit pinned in ion/build_ion.sh
# (network access to github.com is needed at build time for ION and
# PBS_LINK, and to PyPI for cryptography, pytest and matplotlib). The run
# writes results/ (mounted above) and results/REPORT.md.
#
# The three ION nodes use System V shared memory and POSIX named semaphores
# (/dev/shm) inside the container's own IPC namespace, so a container run
# does not interfere with ION on the host. Only one demo runs per container.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git build-essential autoconf automake libtool pkgconf \
      python3 python3-venv python3-pip libssl-dev procps util-linux \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /demo
COPY . /demo

# Build ION, the Python environment, PBS_LINK and Endpoint B into the image;
# run_demo.sh repeats these steps and skips what is already built.
RUN ion/build_ion.sh \
 && python3 -m venv --system-site-packages build/venv-demo \
 && build/venv-demo/bin/python -m pip install -q "cryptography>=41" "pytest>=7" matplotlib \
 && endpoint-python/fetch_pbs_link.sh \
 && make -s -C endpoint-c BUILD=/demo/build/endpoint-c all

CMD ["./run_demo.sh"]
