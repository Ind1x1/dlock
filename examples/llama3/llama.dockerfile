FROM dlock:base as builder

WORKDIR /dlock
COPY ./ .
RUN apt-get update && apt-get install -y protobuf-compiler 
RUN sh scripts/build_wheel.sh

FROM nvcr.io/nvidia/pytorch:24.04-py3  as base

WORKDIR /dlock

RUN apt-get update && apt-get install -y sudo
COPY ./examples/llama3/requirements.txt ./examples/llama3/requirements.txt
RUN pip install -r ./examples/llama3/requirements.txt -i  https://pypi.tuna.tsinghua.edu.cn/simple

COPY --from=builder /dlock/dist/dlock-*.whl /
RUN pip install /*.whl --extra-index-url=https://pypi.org/simple && rm -f /*.whl

COPY ./examples/llama3 ./examples/llama3
