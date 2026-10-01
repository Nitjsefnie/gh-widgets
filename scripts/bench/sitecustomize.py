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


def _dispatch_totals(compact, payload_dir):
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
                "pullRequests": {"totalCount": total["merged_prs"]},
            }
    if not data:
        raise RuntimeError("offline totals query had no fixture aliases")
    return _json_bytes({"data": data})


def _pull_requests_page(compact, variables, payload_dir):
    """The pullRequests connection page this query asks for."""
    payload = _read_json(payload_dir, "pull-requests.json")
    state_match = re.search(r"states\s*:\s*\[([^\]]+)\]", compact)
    states = set()
    if state_match:
        states = {state.strip() for state in
                  state_match.group(1).split(",") if state.strip()}
    if states == {"MERGED"}:
        return payload["merged_page"]
    key = "live_pages" if "MERGED" not in states else "all_pages"
    return _page_for_cursor(payload[key], variables.get("cursor"), "PR")


def _page_for_cursor(pages, cursor, what):
    """The connection page `cursor` asks for: no cursor is page one, and an
    unrecognised one raises rather than serving something the renderer would
    merge as if it were the page it asked for."""
    page_index = 0 if cursor is None else 1
    if page_index >= len(pages):
        raise RuntimeError(f"unknown offline {what} cursor: {cursor!r}")
    return pages[page_index]


def _issues_page(variables, payload_dir):
    """The issues connection page this query asks for."""
    pages = _read_json(payload_dir, "issues.json")["pages"]
    return _page_for_cursor(pages, variables.get("cursor"), "issue")


def _dispatch_profile_orgs(variables, payload_dir):
    """ORG_QUERY: the account's own fields plus a paged org connection.

    Its own payload, because identity.json models the identity query and
    carries no followers — which this query selects.
    """
    payload = _read_json(payload_dir, "profile-orgs.json")
    pages = payload["user"]["organizations"]["pages"]
    connection = _page_for_cursor(pages, variables.get("cursor"),
                                  "profile org")
    return _json_bytes({"data": {"user": {
        **payload["user"], "organizations": connection}}})


def _graphql_dispatch(request, parsed_url, payload_dir):
    query, variables = _graphql_body(request, parsed_url)
    compact = re.sub(r"\s+", " ", query)

    if "repository(" in compact:
        return _dispatch_totals(compact, payload_dir)
    if "repositories(" in compact:
        profile = _read_json(payload_dir, "profile-core.json")
        return _json_bytes({"data": profile})
    if "followers" in compact and "repositories" not in compact:
        # ORG_QUERY: the profile's own fields and the org connection, with no
        # repository connection beside them. That absence is what keeps this
        # branch off REPO_QUERY and off the single combined profile query the
        # baseline release still sends.
        return _dispatch_profile_orgs(variables, payload_dir)
    if ("databaseId" in compact or
            ("organizations" in compact and "repositories" not in compact) or
            ("viewer" in compact and "login" in compact)):
        return _json_bytes({"data": _read_json(payload_dir, "identity.json")})
    if "contributionCalendar" in compact:
        return _json_bytes({"data": _read_json(payload_dir, "calendar.json")})
    if "pullRequests(" in compact or "issues(" in compact:
        # Both are a single page of a paged connection; only the page differs.
        if "pullRequests(" in compact:
            field = "pullRequests"
            connection = _pull_requests_page(compact, variables, payload_dir)
        else:
            field = "issues"
            connection = _issues_page(variables, payload_dir)
        return _json_bytes({"data": {"user": {field: connection}}})
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

    setattr(offline_urlopen, "_gh_bench_shim", True)
    setattr(offline_urlopen, "_gh_bench_original", original)
    urllib.request.urlopen = offline_urlopen


if __name__ == "sitecustomize":
    install()
