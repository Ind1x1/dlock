FROM dlock:ci as builder

WORKDIR /dlock
COPY ./ .
RUN apt-get update && apt-get install -y protobuf-compiler 
RUN sh scripts/build_wheel.sh