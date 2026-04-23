#!/usr/bin/env python3
"""HTTP -> gRPC (dynamic, via server reflection).

Routes:
  GET  /_info
  GET  /_services                        list services exposed by reflection
  POST /{pkg.Service}/{Method}           body = JSON for the request message

Only unary/unary calls are handled -- good enough for a demo.
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from urllib.parse import urlsplit

from shim import read_request, send_json, load_backend

HOST = os.environ.get("INTERCEPT_HOST", "")

_INFO = {
    "handler": "grpc",
    "routes": [
        "GET  /_info",
        "GET  /_services",
        "POST /{pkg.Service}/{Method}    (JSON body -> protobuf request)",
    ],
}


def _channel():
    import grpc
    cfg = load_backend(HOST)
    return grpc.insecure_channel(cfg["address"])


def _list_services(channel) -> list[str]:
    from grpc_reflection.v1alpha.reflection_pb2 import ServerReflectionRequest
    from grpc_reflection.v1alpha.reflection_pb2_grpc import ServerReflectionStub
    stub = ServerReflectionStub(channel)
    req = ServerReflectionRequest(list_services="*")
    for resp in stub.ServerReflectionInfo(iter([req])):
        if resp.HasField("list_services_response"):
            return [svc.name for svc in resp.list_services_response.service]
    return []


def _fetch_file_descriptors(channel, symbol: str):
    """Fetch file descriptor(s) containing the given symbol via reflection."""
    from grpc_reflection.v1alpha.reflection_pb2 import ServerReflectionRequest
    from grpc_reflection.v1alpha.reflection_pb2_grpc import ServerReflectionStub
    from google.protobuf.descriptor_pb2 import FileDescriptorProto
    stub = ServerReflectionStub(channel)
    req = ServerReflectionRequest(file_containing_symbol=symbol)
    out = []
    for resp in stub.ServerReflectionInfo(iter([req])):
        if resp.HasField("file_descriptor_response"):
            for raw in resp.file_descriptor_response.file_descriptor_proto:
                fd = FileDescriptorProto()
                fd.ParseFromString(raw)
                out.append(fd)
    return out


def _build_pool(files):
    """Register FileDescriptorProtos in a fresh pool in dependency order."""
    from google.protobuf import descriptor_pool
    pool = descriptor_pool.DescriptorPool()
    remaining = list(files)
    while remaining:
        made_progress = False
        for fd in list(remaining):
            try:
                pool.Add(fd)
                remaining.remove(fd)
                made_progress = True
            except Exception:
                continue
        if not made_progress:
            pool.Add(remaining[0])
    return pool


def _message_class(pool, descriptor):
    """Return a generated message class for a descriptor (proto 4 & 5 safe)."""
    try:
        from google.protobuf.message_factory import GetMessageClass
        return GetMessageClass(descriptor)
    except ImportError:
        from google.protobuf import message_factory
        return message_factory.MessageFactory(pool).GetPrototype(descriptor)


def handle_call(service_name: str, method_name: str, body: bytes) -> None:
    from google.protobuf import json_format
    channel = _channel()
    try:
        files = _fetch_file_descriptors(channel, service_name)
        if not files:
            return send_json(404, {"error": f"service {service_name} not found"},
                             handler="grpc")
        pool = _build_pool(files)
        svc = pool.FindServiceByName(service_name)
        method = svc.FindMethodByName(method_name)
        if method is None:
            return send_json(404, {"error": f"method {method_name} not found"},
                             handler="grpc")
        if method.client_streaming or method.server_streaming:
            return send_json(400, {"error": "only unary/unary supported"},
                             handler="grpc")
        req_cls = _message_class(pool, method.input_type)
        resp_cls = _message_class(pool, method.output_type)
        req_msg = req_cls()
        if body:
            json_format.Parse(body.decode("utf-8"), req_msg)
        rpc = channel.unary_unary(
            f"/{service_name}/{method_name}",
            request_serializer=req_cls.SerializeToString,
            response_deserializer=resp_cls.FromString,
        )
        resp = rpc(req_msg)
        return send_json(200, json_format.MessageToDict(
            resp, preserving_proto_field_name=True), handler="grpc")
    finally:
        channel.close()


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    p = urlsplit(path).path
    stripped = p.rstrip("/") or "/"

    if method == "GET" and stripped == "/_info":
        try:
            backend = load_backend(HOST)
        except Exception as e:
            backend = {"error": str(e)}
        return send_json(200, {**_INFO, "host": HOST, "backend": backend},
                         handler="grpc")

    if method == "GET" and stripped == "/_services":
        ch = _channel()
        try:
            return send_json(200, {"services": _list_services(ch)}, handler="grpc")
        finally:
            ch.close()

    m = re.fullmatch(r"/([A-Za-z_][A-Za-z0-9_.]*)/([A-Za-z_][A-Za-z0-9_]*)",
                     stripped)
    if m and method == "POST":
        return handle_call(m.group(1), m.group(2), body)

    send_json(404, {"error": f"no route for {method} {stripped}"}, handler="grpc")


def main() -> None:
    try:
        method, path, headers, body = read_request()
    except Exception as e:
        return send_json(400, {"error": f"bad request: {e}"}, handler="grpc")
    try:
        route(method, path, headers, body)
    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        send_json(500, {"error": f"{type(e).__name__}: {e}"}, handler="grpc")


if __name__ == "__main__":
    main()
