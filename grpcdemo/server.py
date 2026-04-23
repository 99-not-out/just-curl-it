"""Trivial helloworld gRPC server, with reflection enabled so the
intercept shim's grpc handler can invoke it without any pre-generated stubs.
"""
from concurrent import futures

import grpc
from grpc_reflection.v1alpha import reflection

import helloworld_pb2
import helloworld_pb2_grpc


class Greeter(helloworld_pb2_grpc.GreeterServicer):
    def SayHello(self, request, context):
        return helloworld_pb2.HelloReply(message=f"Hello, {request.name}!")


def serve() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    helloworld_pb2_grpc.add_GreeterServicer_to_server(Greeter(), server)
    service_names = (
        helloworld_pb2.DESCRIPTOR.services_by_name["Greeter"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(service_names, server)
    server.add_insecure_port("0.0.0.0:50051")
    server.start()
    print("greeter listening on :50051 (reflection enabled)", flush=True)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
