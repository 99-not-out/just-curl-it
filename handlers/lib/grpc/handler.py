#!/usr/bin/env python3
"""HTTP -> gRPC, dynamically, via server reflection. No stubs, no .proto.

  GET  /_services                    services the server advertises
  POST /{pkg.Service}/{Method}       body = JSON for the request message -> JSON reply

Unary calls only. The server must have reflection enabled.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import jci

ROUTES = [
    "GET  /_info",
    "GET  /_services",
    "POST /{pkg.Service}/{Method}    (JSON body -> protobuf request; unary only)",
]


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def channel(cfg: dict):
    import grpc
    address = cfg.get("address", "").strip()
    if not address:
        raise jci.BackendConfigError("missing 'address' in backends.conf")
    if _truthy(cfg.get("tls")):
        root = None
        if cfg.get("ca_cert"):
            with open(cfg["ca_cert"], "rb") as f:
                root = f.read()
        return grpc.secure_channel(address, grpc.ssl_channel_credentials(root_certificates=root))
    return grpc.insecure_channel(address)


def _list_services(ch, t: float) -> list[str]:
    from grpc_reflection.v1alpha.reflection_pb2 import ServerReflectionRequest
    from grpc_reflection.v1alpha.reflection_pb2_grpc import ServerReflectionStub
    stub = ServerReflectionStub(ch)
    for resp in stub.ServerReflectionInfo(iter([ServerReflectionRequest(list_services="*")]), timeout=t):
        if resp.HasField("list_services_response"):
            return [s.name for s in resp.list_services_response.service]
    return []


def _file_descriptors(ch, symbol: str, t: float):
    from google.protobuf.descriptor_pb2 import FileDescriptorProto
    from grpc_reflection.v1alpha.reflection_pb2 import ServerReflectionRequest
    from grpc_reflection.v1alpha.reflection_pb2_grpc import ServerReflectionStub
    stub = ServerReflectionStub(ch)
    out = []
    for resp in stub.ServerReflectionInfo(
            iter([ServerReflectionRequest(file_containing_symbol=symbol)]), timeout=t):
        if resp.HasField("error_response"):
            return []
        if resp.HasField("file_descriptor_response"):
            for raw in resp.file_descriptor_response.file_descriptor_proto:
                fd = FileDescriptorProto()
                fd.ParseFromString(raw)
                out.append(fd)
    return out


def _build_pool(files):
    """Register descriptors in dependency order (retry until no progress)."""
    from google.protobuf import descriptor_pool
    pool = descriptor_pool.DescriptorPool()
    remaining = list(files)
    while remaining:
        progressed = False
        for fd in list(remaining):
            try:
                pool.Add(fd)
                remaining.remove(fd)
                progressed = True
            except Exception:
                continue
        if not progressed:
            pool.Add(remaining[0])
    return pool


def _message_class(pool, descriptor):
    try:
        from google.protobuf.message_factory import GetMessageClass
        return GetMessageClass(descriptor)
    except ImportError:  # protobuf < 4.21
        from google.protobuf import message_factory
        return message_factory.MessageFactory(pool).GetPrototype(descriptor)


def handle_call(cfg: dict, service: str, method_name: str, body: bytes) -> None:
    import grpc
    from google.protobuf import json_format
    t = jci.timeout(cfg)
    ch = channel(cfg)
    try:
        files = _file_descriptors(ch, service, t)
        if not files:
            return jci.send_error(404, f"service {service} not found via reflection")
        pool = _build_pool(files)
        try:
            svc = pool.FindServiceByName(service)
        except KeyError:
            return jci.send_error(404, f"service {service} not in the reflected descriptors")
        # FindMethodByName returns None on protobuf 4 and raises on 5; avoid both.
        method = next((m for m in svc.methods if m.name == method_name), None)
        if method is None:
            return jci.send_error(404, f"method {method_name} not found on {service}",
                                  methods=[m.name for m in svc.methods])
        if method.client_streaming or method.server_streaming:
            return jci.send_error(501, "only unary/unary methods are supported")
        req_cls, resp_cls = _message_class(pool, method.input_type), _message_class(pool, method.output_type)
        req = req_cls()
        if body:
            try:
                json_format.Parse(body.decode("utf-8"), req)
            except (json_format.ParseError, UnicodeDecodeError, ValueError) as e:
                return jci.send_error(400, f"bad request body: {e}")
        rpc = ch.unary_unary(f"/{service}/{method_name}",
                             request_serializer=req_cls.SerializeToString,
                             response_deserializer=resp_cls.FromString)
        try:
            reply = rpc(req, timeout=t)
        except grpc.RpcError as e:
            return jci.send_error(502, f"{e.code().name}: {e.details()}", grpc_code=e.code().name)
        jci.send_json(200, json_format.MessageToDict(reply, preserving_proto_field_name=True))
    finally:
        ch.close()


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    p = urlsplit(path).path.rstrip("/") or "/"
    if method == "GET" and p == "/_info":
        return jci.info("rpc", ROUTES)

    cfg = jci.load_backend()

    if method == "GET" and p == "/_services":
        ch = channel(cfg)
        try:
            return jci.send_json(200, {"services": _list_services(ch, jci.timeout(cfg))})
        finally:
            ch.close()

    m = re.fullmatch(r"/([A-Za-z_][A-Za-z0-9_.]*)/([A-Za-z_][A-Za-z0-9_]*)", p)
    if m and method == "POST":
        return handle_call(cfg, m.group(1), m.group(2), body)

    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
