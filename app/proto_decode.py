"""Lightweight wrappers around opentelemetry-proto for decoding OTLP payloads.

We import lazily inside helpers so the rest of the app can be inspected
without the proto package being present (useful for tests).
"""

from __future__ import annotations

from typing import Any


def _attr_value(attr_value) -> Any:
    """Convert an OTLP AnyValue proto to a Python primitive."""
    which = attr_value.WhichOneof("value")
    if which == "string_value":
        return attr_value.string_value
    if which == "int_value":
        return attr_value.int_value
    if which == "double_value":
        return attr_value.double_value
    if which == "bool_value":
        return attr_value.bool_value
    if which == "array_value":
        return [_attr_value(v) for v in attr_value.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _attr_value(kv.value) for kv in attr_value.kvlist_value.values}
    if which == "bytes_value":
        return attr_value.bytes_value
    return None


def _attrs_to_dict(attrs) -> dict[str, Any]:
    return {kv.key: _attr_value(kv.value) for kv in attrs}


def decode_metrics(body: bytes) -> list[dict]:
    """Parse an OTLP ExportMetricsServiceRequest into a flat list of points.

    Each point dict has: { name, unit, attrs, value, timestamp_ns }
    """
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
        ExportMetricsServiceRequest,
    )

    req = ExportMetricsServiceRequest()
    req.ParseFromString(body)

    points: list[dict] = []
    for resource_metrics in req.resource_metrics:
        resource_attrs = _attrs_to_dict(resource_metrics.resource.attributes)
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                name = metric.name
                unit = metric.unit
                data_kind = metric.WhichOneof("data")
                data_points = []
                if data_kind == "sum":
                    data_points = metric.sum.data_points
                elif data_kind == "gauge":
                    data_points = metric.gauge.data_points
                elif data_kind == "histogram":
                    data_points = metric.histogram.data_points

                for dp in data_points:
                    attrs = dict(resource_attrs)
                    attrs.update(_attrs_to_dict(dp.attributes))

                    if data_kind in ("sum", "gauge"):
                        value = (
                            dp.as_int
                            if dp.WhichOneof("value") == "as_int"
                            else dp.as_double
                        )
                    elif data_kind == "histogram":
                        value = dp.sum
                    else:
                        value = None

                    points.append({
                        "name": name,
                        "unit": unit,
                        "attrs": attrs,
                        "value": value,
                        "timestamp_ns": dp.time_unix_nano,
                    })
    return points


def decode_logs(body: bytes) -> list[dict]:
    """Parse an OTLP ExportLogsServiceRequest into a flat list of log records.

    Each record dict has: { event_name, attrs, body, timestamp_ns }
    """
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
        ExportLogsServiceRequest,
    )

    req = ExportLogsServiceRequest()
    req.ParseFromString(body)

    records: list[dict] = []
    for resource_logs in req.resource_logs:
        resource_attrs = _attrs_to_dict(resource_logs.resource.attributes)
        for scope_logs in resource_logs.scope_logs:
            for record in scope_logs.log_records:
                attrs = dict(resource_attrs)
                attrs.update(_attrs_to_dict(record.attributes))
                event_name = (
                    record.event_name
                    if hasattr(record, "event_name") and record.event_name
                    else attrs.get("event.name")
                )
                body_val = _attr_value(record.body) if record.HasField("body") else None
                records.append({
                    "event_name": event_name,
                    "attrs": attrs,
                    "body": body_val,
                    "timestamp_ns": record.time_unix_nano or record.observed_time_unix_nano,
                })
    return records
