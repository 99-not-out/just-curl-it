FROM python:3.12-slim

RUN pip install --no-cache-dir \
      grpcio==1.66.2 \
      grpcio-tools==1.66.2 \
      grpcio-reflection==1.66.2

WORKDIR /app
COPY helloworld.proto .
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. helloworld.proto

COPY server.py .

EXPOSE 50051
CMD ["python", "-u", "server.py"]
