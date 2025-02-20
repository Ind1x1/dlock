PYTHON = python
CXX = g++

all: python_pb go_pb

python_pb: dlock/proto/elastic_training.proto dlock/proto/brain.proto
	/bin/sh scripts/build_proto.sh

go_pb: dlock/proto/brain.proto
	protoc -I. dlock/proto/brain.proto --go_out=plugins=grpc:.
