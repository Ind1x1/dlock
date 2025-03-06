FROM dlock:base as builder

WORKDIR /dlock
COPY ./ .
RUN apt-get update && apt-get install -y protobuf-compiler 
RUN sh scripts/build_wheel.sh

FROM python:3.8.14 as base
RUN pip install pyparsing -i https://pypi.org/simple

RUN apt-get -qq update && apt-get install -y iputils-ping vim gdb

ENV VERSION="0.3.8"
COPY --from=builder /dlock/dist/dlock-${VERSION}-py3-none-any.whl /
RUN pip install /dlock-${VERSION}-py3-none-any.whl[k8s] --extra-index-url=https://pypi.org/simple && rm -f /*.whl
RUN unset VERSION

RUN pip3 install protobuf==4.25.3 grpcio==1.62.1 grpcio-tools==1.58.0 -i https://pypi.tuna.tsinghua.edu.cn/simple
