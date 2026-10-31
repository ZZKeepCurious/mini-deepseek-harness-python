"""插件市场 index format 1 解析与安装参数（对齐 @deepseek-ai/dsh-plugin-market-index）。

mini 载体在 npm/git 之外接受 `source.type === "python"`，用于 Cordis 模块 /
可选 pip 包名（登记 verified-diffs §3 pluginMarket Python 扩展）。
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

__all__ = [
    "MarketIndexError",
    "market_install_target",
    "parse_market_index",
]

_LANGUAGE_ID = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$", re.I)
_PACKAGE_NAME = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$", re.I)
_SEMVER = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class MarketIndexError(Exception):
    def __init__(self, path: str, reason: str):
        super().__init__(f"{path} {reason}")
        self.path = path
        self.reason = reason


def _fail(path: str, reason: str) -> None:
    raise MarketIndexError(path, reason)


def _localized(value: Any, path: str) -> Any:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        text: dict[str, str] = {}
        for language, member in value.items():
            if not _LANGUAGE_ID.match(language):
                _fail(f"{path}.{language}", "must be keyed by a lowercase language id")
            if not isinstance(member, str) or not member.strip():
                _fail(f"{path}.{language}", "must be a non-empty string")
            text[language.lower()] = member
        if "en" not in text:
            _fail(path, "must contain en")
        return text
    _fail(path, "must be localized text")


def _source(value: Any, path: str) -> dict:
    obj = value if isinstance(value, dict) else _fail(path, "must be an object")
    kind = obj.get("type")
    if kind == "npm":
        registry = obj.get("registry")
        if registry is not None and (not isinstance(registry, str) or not registry.strip()):
            _fail(f"{path}.registry", "must be a string")
        return {"type": "npm", **({} if registry is None else {"registry": registry})}
    if kind == "git":
        url = obj.get("url")
        commit = obj.get("commit")
        if not isinstance(url, str) or not url.startswith("https://"):
            _fail(f"{path}.url", "must be an https URL")
        if not isinstance(commit, str) or not _COMMIT.match(commit):
            _fail(f"{path}.commit", "must be a lowercase 40-hex-digit commit SHA")
        package_path = obj.get("path")
        if package_path is not None and not isinstance(package_path, str):
            _fail(f"{path}.path", "must be a string")
        return {
            "type": "git",
            "url": url,
            "commit": commit,
            **({} if package_path is None else {"path": package_path}),
        }
    if kind == "python":
        module = obj.get("module")
        if not isinstance(module, str) or not module.strip():
            _fail(f"{path}.module", "must be a non-empty module path")
        pip_spec = obj.get("pip")
        if pip_spec is not None and (not isinstance(pip_spec, str) or not pip_spec.strip()):
            _fail(f"{path}.pip", "must be a string")
        return {
            "type": "python",
            "module": module.strip(),
            **({} if pip_spec is None else {"pip": pip_spec.strip()}),
        }
    _fail(f"{path}.type", 'must be "npm", "git", or "python"')


def _version(value: Any, path: str) -> dict:
    obj = value if isinstance(value, dict) else _fail(path, "must be an object")
    version = obj.get("version")
    if not isinstance(version, str) or not _SEMVER.match(version):
        _fail(f"{path}.version", "must be an exact SemVer version")
    publish = obj.get("publishTimestamp")
    if not isinstance(publish, int) or publish < 0 or publish >= 10**11:
        _fail(f"{path}.publishTimestamp",
              "must be an integer number of Unix seconds in [0, 10^11)")
    engines = obj.get("engines")
    dsh_range = None
    if engines is not None:
        if not isinstance(engines, dict):
            _fail(f"{path}.engines", "must be an object")
        dsh_range = engines.get("dsh")
        if dsh_range is not None and (not isinstance(dsh_range, str) or dsh_range.startswith("workspace:")):
            _fail(f"{path}.engines.dsh", "must be a SemVer range")
    return {
        "version": version,
        "publishTimestamp": publish,
        "source": _source(obj.get("source"), f"{path}.source"),
        "engines": {} if dsh_range is None else {"dsh": dsh_range},
    }


def _metadata(value: Any, path: str) -> dict:
    obj = value if isinstance(value, dict) else _fail(path, "must be an object")
    categories = obj.get("categories") or {}
    labels = obj.get("labels") or {}
    sort_keys = obj.get("sortKeys") or {}
    if not isinstance(categories, dict) or not isinstance(labels, dict) or not isinstance(sort_keys, dict):
        _fail(path, "metadata groups must be objects")
    return {"categories": categories, "labels": labels, "sortKeys": sort_keys}


def _plugin(value: Any, path: str, index_url: str) -> dict:
    obj = value if isinstance(value, dict) else _fail(path, "must be an object")
    name = obj.get("name")
    if not isinstance(name, str) or len(name) > 214 or not _PACKAGE_NAME.match(name):
        _fail(f"{path}.name", "must be a valid package name")
    versions_raw = obj.get("versions")
    if not isinstance(versions_raw, list) or not versions_raw:
        _fail(f"{path}.versions", "must be a non-empty array")
    seen: set[str] = set()
    versions = []
    for position, member in enumerate(versions_raw):
        entry = _version(member, f"{path}.versions[{position}]")
        if entry["version"] in seen:
            _fail(f"{path}.versions[{position}].version",
                  f"duplicates {entry['version']}")
        seen.add(entry["version"])
        versions.append(entry)
    versions.sort(key=lambda item: _semver_key(item["version"]), reverse=True)
    base = urlparse(index_url)
    icon = obj.get("icon")
    icon_url = None
    if icon is not None:
        if not isinstance(icon, str) or not icon.strip():
            _fail(f"{path}.icon", "must be a string")
        if icon.startswith("data:"):
            icon_url = icon
        else:
            resolved = urlparse(urljoin(index_url, icon))
            if resolved.scheme not in ("https", "http") and resolved.netloc != base.netloc:
                _fail(f"{path}.icon", "must resolve to https or the index origin")
            icon_url = resolved.geturl()
    out: dict[str, Any] = {"name": name, "versions": versions}
    for key, reader in (
        ("title", _localized),
        ("description", _localized),
        ("author", lambda v, p: v if isinstance(v, str) and v.strip() else _fail(p, "must be a string")),
    ):
        if key in obj:
            out[key] = reader(obj[key], f"{path}.{key}")
    if icon_url is not None:
        out["icon"] = icon_url
    if "keywords" in obj:
        keywords = obj["keywords"]
        if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
            _fail(f"{path}.keywords", "must be an array of strings")
        out["keywords"] = keywords
    if "labels" in obj:
        labels = obj["labels"]
        if not isinstance(labels, list) or not all(isinstance(k, str) for k in labels):
            _fail(f"{path}.labels", "must be an array of strings")
        out["labels"] = labels
    if "category" in obj and isinstance(obj["category"], str):
        out["category"] = obj["category"]
    return out


def _semver_key(version: str) -> tuple:
    core, _, _ = version.partition("+")
    core, _, prerelease = core.partition("-")
    parts = [int(p) for p in core.split(".")]
    return (*parts, prerelease)


def parse_market_index(value: Any, index_url: str) -> dict:
    """校验 format 1 文档；失败抛 MarketIndexError。"""
    if not index_url or urlparse(index_url).scheme not in ("http", "https"):
        _fail("$", f"index URL {json.dumps(index_url)} must be an absolute URL")
    root = value if isinstance(value, dict) else _fail("$", "must be an object")
    if root.get("format") != 1:
        _fail("$.format", "must be 1")
    title = _localized(root.get("title"), "$.title")
    metadata = _metadata(root.get("metadata", {}), "$.metadata")
    plugins_raw = root.get("plugins")
    if not isinstance(plugins_raw, list):
        _fail("$.plugins", "must be an array")
    names: set[str] = set()
    plugins = []
    for position, entry in enumerate(plugins_raw):
        parsed = _plugin(entry, f"$.plugins[{position}]", index_url)
        if parsed["name"] in names:
            _fail(f"$.plugins[{position}].name", f"duplicates {parsed['name']}")
        names.add(parsed["name"])
        plugins.append(parsed)
    out: dict[str, Any] = {"format": 1, "title": title, "metadata": metadata, "plugins": plugins}
    if "description" in root:
        out["description"] = _localized(root["description"], "$.description")
    return out


def parse_market_index_bytes(body: bytes, index_url: str) -> dict:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MarketIndexError("$", "must be valid UTF-8 JSON") from error
    return parse_market_index(value, index_url)


def market_install_target(name: str, version: dict) -> dict:
    source = version["source"]
    if source["type"] == "npm":
        out = {"spec": f"{name}@{version['version']}"}
        if "registry" in source:
            out["registry"] = source["registry"]
        return out
    if source["type"] == "python":
        out = {
            "kind": "python",
            "spec": f"python-module:{source['module']}",
            "module": source["module"],
        }
        if "pip" in source:
            out["pip"] = source["pip"]
        return out
    repository = source["url"]
    if not repository.endswith(".git"):
        repository = f"{repository}.git"
    spec = f"git+{repository}#{source['commit']}"
    if "path" in source:
        spec = f"{spec}&path:/{source['path']}"
    return {"spec": spec}
