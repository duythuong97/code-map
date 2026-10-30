"""API Gateway routes from CloudFormation (``AWS::ApiGateway::Resource`` / ``::Method``).

A client calls the gateway's public path; the gateway forwards it to the
integration ``Uri`` of a backend in another VPC. Each method becomes an
``API_OPERATION`` with ``gateway: true`` and its backend method and path in
``integration``; the linker's ``gateway`` resolver adds the ``ROUTES_TO`` edge to
the backend operation, and ``link_api`` links client calls to the public path.

The public path is the chain of ``PathPart`` values from the method's resource up
to the API root (a parent that is not a resource of these templates, such as
``!GetAtt Api.RootResourceId`` or an imported id). Resources may be declared in
another template of the same source.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from code_tree_exporter.contract.graph_contract import api_operation_id, normalize_http_route
from code_tree_exporter.extractors.package_support.package_writer import (
    PackageBuilder,
    SourceFile,
    configured_files,
    line_text,
)

TEMPLATE_SUFFIXES = (".yml", ".yaml", ".json", ".template")
_RESOURCE = "AWS::ApiGateway::Resource"
_METHOD = "AWS::ApiGateway::Method"
_SKIPPED_METHODS = frozenset({"OPTIONS"})  # CORS preflight answered by the gateway
_SKIPPED_INTEGRATIONS = frozenset({"MOCK"})
_URL_ORIGIN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*")


class _TemplateLoader(yaml.SafeLoader):
    """YAML with the CloudFormation short-form tags (``!Ref X`` -> ``{"Ref": "X"}``)."""


def _intrinsic(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> dict:
    name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value: object = loader.construct_scalar(node)
        if suffix == "GetAtt" and isinstance(value, str):
            value = value.split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {name: value}


_TemplateLoader.add_multi_constructor("!", _intrinsic)


@dataclass(frozen=True)
class _Resource:
    file: SourceFile
    logical_id: str
    parent: object
    path_part: str


@dataclass(frozen=True)
class _Method:
    file: SourceFile
    logical_id: str
    resource: object
    http_method: str
    integration: dict


def load_template(text: str) -> dict:
    """The template as plain data, JSON or YAML; ``{}`` when it is neither."""
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = yaml.load(text, Loader=_TemplateLoader)  # noqa: S506 - SafeLoader subclass
        except yaml.YAMLError:
            return {}
    return data if isinstance(data, dict) else {}


def extract(config: dict) -> None:
    if config.get("type") != "api-gateway":
        raise ValueError("Config type must be api-gateway")
    source = config["source"]
    repository = config.get("repository", source)
    system = config.get("system", source)
    application = config.get("application", source)
    files = configured_files(config, TEMPLATE_SUFFIXES)
    builder = PackageBuilder(
        f"api-gateway-{source}",
        f"extractor:api-gateway/{source}",
        "api-gateway-extractor",
        "1.0.0",
        {
            "source": source,
            "repository": repository,
            "technology": "AWS CloudFormation (YAML/JSON)",
            "extractorContract": "3.0",
            "capabilities": ["gateway-routes", "gateway-integrations"],
        },
    )
    builder.files_scanned = len(files)
    resources: dict[str, list[_Resource]] = {}
    methods: list[_Method] = []
    for file in files:
        for logical_id, resource in _resources(load_template(file.text)):
            kind = resource.get("Type")
            properties = resource.get("Properties") if isinstance(resource.get("Properties"), dict) else {}
            if kind == _RESOURCE:
                resources.setdefault(logical_id, []).append(
                    _Resource(file, logical_id, properties.get("ParentId"), str(properties.get("PathPart") or ""))
                )
            elif kind == _METHOD:
                integration = properties.get("Integration") if isinstance(properties.get("Integration"), dict) else {}
                methods.append(
                    _Method(file, logical_id, properties.get("ResourceId"), str(properties.get("HttpMethod") or ""), integration)
                )

    app_id = f"api-application:{application}"
    builder.add_node(app_id, "API_APPLICATION", application, application, application,
                     system_key=system, repository_key=repository, properties={"gateway": True})
    for method in sorted(methods, key=lambda item: (item.file.relative, item.logical_id)):
        _add_method(builder, method, resources, application, app_id, system, repository)
    builder.write(Path(config["output"]))


def _resources(template: dict) -> list[tuple[str, dict]]:
    items = template.get("Resources")
    if not isinstance(items, dict):
        return []
    return [(str(key), value) for key, value in items.items() if isinstance(value, dict)]


def _add_method(builder, method: _Method, resources, application, app_id, system, repository) -> None:
    http_method = method.http_method.upper()
    integration_type = str(method.integration.get("Type") or "").upper()
    if not http_method or http_method in _SKIPPED_METHODS or integration_type in _SKIPPED_INTEGRATIONS:
        return
    line = _line_of(method.file.text, method.logical_id)
    path, unresolved = _public_path(method.resource, method.file, resources)
    if unresolved:
        builder.add_issue(
            "GATEWAY_RESOURCE_NOT_FOUND",
            "WARNING",
            f"Gateway method {method.logical_id} names resource {unresolved}, which no template of this source declares",
            raw_reference=unresolved,
            source_path=method.file.relative,
            start_line=line,
        )
        return
    # ANY is a gateway wildcard; keep it so the route still shows, a client call links by its own verb.
    method_name, route = normalize_http_route(http_method, path)
    operation_id = api_operation_id(application, method_name, route)
    properties: dict[str, object] = {
        "method": method_name,
        "route": route,
        "gateway": True,
        "integrationType": integration_type,
        "logicalId": method.logical_id,
    }
    backend = _backend(method.integration, http_method)
    if backend:
        properties["integration"] = backend
    builder.add_node(operation_id, "API_OPERATION", f"{method_name} {route}", f"{application}.{method_name}.{route}",
                     f"{method_name} {route}", system_key=system, repository_key=repository, properties=properties)
    builder.add_edge(app_id, operation_id, "CONTAINS", graph_layer="STRUCTURAL")
    builder.add_evidence("NODE", operation_id, method.file.relative, line, line, "DECLARATION",
                         line_text(method.file.text, line))


def _public_path(reference: object, file: SourceFile, resources: dict[str, list[_Resource]]) -> tuple[str, str]:
    """("/a/b", "") from the method's resource up to the API root, or ("", missing logical id)."""
    parts: list[str] = []
    seen: set[str] = set()
    current = _ref(reference)
    if current is None:
        return "/", ""  # a method on the root resource itself
    while current is not None and current not in seen:
        seen.add(current)
        candidates = resources.get(current)
        if not candidates:
            # The first hop must be a declared resource; above it, an unknown parent is the root.
            return ("", current) if not parts else ("/" + "/".join(reversed(parts)), "")
        resource = next((item for item in candidates if item.file == file), candidates[0])
        parts.append(resource.path_part)
        current = _ref(resource.parent)
    return "/" + "/".join(reversed(parts)), ""


def _ref(value: object) -> str | None:
    if isinstance(value, dict) and isinstance(value.get("Ref"), str):
        return value["Ref"]
    return None


def _backend(integration: dict, http_method: str) -> dict[str, str] | None:
    uri = _text(integration.get("Uri"))
    if not uri:
        return None
    path = _URL_ORIGIN.sub("", uri.strip()) or "/"
    path = path.split("?", 1)[0]
    method = str(integration.get("IntegrationHttpMethod") or http_method).upper()
    if method == "ANY":
        method = http_method.upper()
    try:
        method, path = normalize_http_route(method, path)
    except ValueError:
        return None
    return {"method": method, "path": path, "uri": uri}


def _text(value: object) -> str:
    """A string with every ``${...}`` it cannot know left in place."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "Fn::Sub" in value:
            sub = value["Fn::Sub"]
            return _text(sub[0] if isinstance(sub, list) and sub else sub)
        if "Fn::Join" in value:
            joined = value["Fn::Join"]
            if isinstance(joined, list) and len(joined) == 2 and isinstance(joined[1], list):
                return str(joined[0]).join(_text(part) or "${?}" for part in joined[1])
        if "Ref" in value:
            return "${" + str(value["Ref"]) + "}"
    return ""


def _line_of(text: str, logical_id: str) -> int:
    pattern = re.compile(rf'^\s*"?{re.escape(logical_id)}"?\s*:', re.MULTILINE)
    match = pattern.search(text)
    return text.count("\n", 0, match.start()) + 1 if match else 1
