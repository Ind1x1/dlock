FROM python:3.8.14
ARG EXTRA_PYPI_INDEX=https://pypi.org/simple

# Allows for log messages by `print` in Python to be immediately dumped
# to the stream instead of being buffered.
ENV PYTHONUNBUFFERED 0

RUN chmod a+rx /etc/bash.bashrc

RUN sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list && \
    sed -i 's|http://security.debian.org|http://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list && \
    apt-get update && apt-get install -y \
        unzip \
        curl \
        git \
        software-properties-common \
        g++ \
        wget \
        cmake \
        vim \
        net-tools \
        ca-certificates \
        shellcheck \
        clang-format > /dev/null && \
    python -m pip install --quiet --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/

# Install Go and related tools
ARG GO_MIRROR_URL=https://dl.google.com/go
ENV GOPATH /root/go
ENV PATH /usr/local/go/bin:$GOPATH/bin:$PATH
COPY docker/scripts/install-go.bash /
RUN chmod +x /install-go.bash && /install-go.bash ${GO_MIRROR_URL} && rm /install-go.bash

# Install protobuf and protoc
COPY docker/scripts/install-protobuf.bash /
RUN  chmod +x /install-protobuf.bash && rm /install-protobuf.bash

# Install Pre-commit
RUN pip install pre-commit pytest kubernetes grpcio-tools psutil \
    deprecated -i https://mirrors.aliyun.com/pypi/simple/

# Configure envtest for integration tests of kubebuilder
ENV KUBEBUILDER_CONTROLPLANE_START_TIMEOUT 60s
COPY docker/scripts/install-kube-envtest.bash /
RUN chmod +x /install-kube-envtest.bash && /install-kube-envtest.bash 1.19.2 && rm /install-kube-envtest.bash
