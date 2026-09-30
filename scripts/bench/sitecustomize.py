"""Route renderer GitHub API requests to deterministic local JSON fixtures."""
import io
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path


TOTALS_ALIAS = re.compile(
    r'(?P<alias>[A-Za-z_][A-Za-z0-9_]*):\s*repository\s*\('
    r'\s*owner\s*:\s*"(?P<owner>[^"]+)"\s*,\s*'
    r'name\s*:\s*"(?P<name>[^"]+)"')


class FixtureResponse:
    """The small urlopen response surface used by the renderers."""

    def __init__(self, body):
        self._stream = io.BytesIO(body)
        self.headers = {}

    def read(self, size=-1):
        return self._stream.read(size)

    def close(self):
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()


def _request_url(request):
    if hasattr(request, "full_url"):
        return request.full_url
    return str(request)


def _graphql_body(request, parsed_url):
    data = getattr(request, "data", None)
    if data:
        decoded = data.decode("utf-8") if isinstance(data, bytes) else data
        body = json.loads(decoded)
        return body.get("query", ""), body.get("variables", {})
    query = urllib.parse.parse_qs(parsed_url.query).get("query", [""])[0]
    variables = urllib.parse.parse_qs(parsed_url.query).get("variables", ["{}"])[0]
    return query, json.loads(variables)


def _read_json(payload_dir, name):
    return json.loads((Path(payload_dir) / name).read_text(encoding="utf-8"))


def _json_bytes(payload):
    return json.dumps(payload, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _graphql_dispatch(request, parsed_url, payload_dir):
    query, variables = _graphql_body(request, parsed_url)
    compact = re.sub(r"\s+", " ", query)

    if "repository(" in compact:
        totals = _read_json(payload_dir, "totals.json")
        data = {}
        for match in TOTALS_ALIAS.finditer(compact):
            repo = f"{match.group('owner')}/{match.group('name')}"
            if repo in totals:
                total = totals[repo]
                data[match.group("alias")] = {
                    "defaultBranchRef": {
                        "name": total["branch"],
                        "target": {"oid": total["head"]},
                    },
                    "issues": {"totalCount": total["issues"]},
                    "pullRequests": {
                        "totalCount": total["merged_prs"]},
                }
        if not data:
            raise RuntimeError("offline totals query had no fixture aliases")
        return _json_bytes({"data": data})

    if "repositories(" in compact:
        return _json_bytes({"data": _read_json(payload_dir,
                                              "profile-core.json")})

    if ("databaseId" in compact or
            ("organizations" in compact and "repositories" not in compact) or
            ("viewer" in compact and "login" in compact)):
        return _json_bytes({"data": _read_json(payload_dir, "identity.json")})

    if "contributionCalendar" in compact:
        return _json_bytes({"data": _read_json(payload_dir, "calendar.json")})

    if "pullRequests(" in compact:
        payload = _read_json(payload_dir, "pull-requests.json")
        state_match = re.search(r"states\s*:\s*\[([^\]]+)\]", compact)
        states = set()
        if state_match:
            states = {state.strip() for state in
                      state_match.group(1).split(",") if state.strip()}
        if states == {"MERGED"}:
            connection = payload["merged_page"]
        else:
            key = "live_pages" if "MERGED" not in states else "all_pages"
            pages = payload[key]
            cursor = variables.get("cursor")
            page_index = 0 if cursor is None else 1
            if page_index >= len(pages):
                raise RuntimeError(f"unknown offline PR cursor: {cursor!r}")
            connection = pages[page_index]
        return _json_bytes({"data": {"user": {
            "pullRequests": connection}}})

    if "issues(" in compact:
        payload = _read_json(payload_dir, "issues.json")
        cursor = variables.get("cursor")
        page_index = 0 if cursor is None else 1
        pages = payload["pages"]
        if page_index >= len(pages):
            raise RuntimeError(f"unknown offline issue cursor: {cursor!r}")
        return _json_bytes({"data": {"user": {"issues": pages[page_index]}}})

    raise RuntimeError("unmatched offline GraphQL fixture query")


def dispatch(request, payload_dir):
    """Return fixture bytes for api.github.com, else None for pass-through."""
    parsed_url = urllib.parse.urlsplit(_request_url(request))
    if parsed_url.hostname != "api.github.com":
        return None

    if parsed_url.path == "/graphql":
        return _graphql_dispatch(request, parsed_url, payload_dir)

    path = parsed_url.path.rstrip("/").split("/")
    if len(path) == 4 and path[1] == "users" and path[3] == "orgs":
        return (Path(payload_dir) / "orgs.json").read_bytes()

    raise RuntimeError(f"unmatched offline GitHub API path: {parsed_url.path}")


def install():
    """Install the network shim when imported by Python as sitecustomize."""
    original = urllib.request.urlopen
    if getattr(original, "_gh_bench_shim", False):
        return

    def offline_urlopen(request, *args, **kwargs):
        if urllib.parse.urlsplit(_request_url(request)).hostname == "api.github.com":
            payload_dir = os.environ["GH_BENCH_PAYLOADS"]
            return FixtureResponse(dispatch(request, payload_dir))
        return original(request, *args, **kwargs)

    offline_urlopen._gh_bench_shim = True
    offline_urlopen._gh_bench_original = original
    urllib.request.urlopen = offline_urlopen


if __name__ == "sitecustomize":
    install()
