"""Only orgs the account administers are its own: a plain membership feeds no card."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests import _ghw_temp
from tests.test_popularity import USER, popularity, repo, response
from tests.test_render import FakeAPI, complete_cache, render

common = render.common
FIELD = "viewerCanAdminister"
ADMIN = {"login": "AdminOrg", FIELD: True}
MEMBER = {"login": "MemberOrg", FIELD: False}
WORKFLOW_TOKEN = "github-actions[bot]"


def as_requested(answer, viewer):
    """GitHub as `viewer`'s token sees it: only requested fields come back, and
    viewerCanAdminister asked of another account's orgs fails the whole query."""
    def gql(token, query, variables=None, **kwargs):
        data = answer(token, query, variables, **kwargs)
        if "viewer { login }" in query:
            data["viewer"] = {"login": viewer}
        gated = f"{FIELD} @include(if: $self)" in query
        requested = FIELD in query and (not gated or (variables or {}).get("self") is True)
        user = data.get("user") or {}
        nodes = (user.get("organizations") or {}).get("nodes", [])
        if requested and nodes and user["login"].casefold() != viewer.casefold():
            raise RuntimeError("GraphQL errors: Resource not accessible by integration")
        if not requested:
            for node in nodes:
                node.pop(FIELD, None)
        return data
    return gql


def identity_answer(nodes):
    def answer(token, query, variables=None, **_kwargs):
        return {"user": {"login": "Octocat", "databaseId": 42, "organizations": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [dict(node) for node in nodes]}}}
    return answer


def pr_to(owner):
    return {"merged": True, "repository": {
        "nameWithOwner": f"{owner}/thing", "isPrivate": False, "owner": {"login": owner}}}


class Identity(unittest.TestCase):
    def test_a_member_org_is_neither_an_org_nor_an_insider(self):
        me = common.fetch_identity("t", "octocat", include_environment=False,
                                   gql_fn=as_requested(identity_answer([ADMIN, MEMBER]), "Octocat"))
        self.assertEqual(me.orgs, ["AdminOrg"])
        self.assertEqual(me.insiders, {"octocat", "adminorg"})

    def test_another_viewer_never_asks_the_flag_and_keeps_the_visible_orgs(self):
        me = common.fetch_identity("t", "octocat", include_environment=False,
                                   gql_fn=as_requested(identity_answer([ADMIN, MEMBER]), WORKFLOW_TOKEN))
        self.assertEqual(me.orgs, ["AdminOrg", "MemberOrg"])
        self.assertEqual(me.insiders, {"octocat", "adminorg", "memberorg"})

    def test_the_public_snapshot_view_keeps_only_administered_public_orgs(self):
        nodes = [{**ADMIN, "isPublic": True}, {**MEMBER, "isPublic": True}]
        me = common.fetch_identity("t", "octocat", include_environment=False, include_public_orgs=True,
                                   gql_fn=as_requested(identity_answer(nodes), "Octocat"))
        self.assertEqual(me.public_orgs, ["AdminOrg"])
        # The production path reads public memberships unauthenticated, which knows no roles.
        github = as_requested(identity_answer([ADMIN, MEMBER]), "Octocat")
        with mock.patch.object(common, "gql", side_effect=github), \
                mock.patch.object(common, "fetch_public_organizations", return_value=["ADMINORG", "MemberOrg"]):
            me = common.fetch_identity("t", "octocat", include_environment=False, include_public_orgs=True)
        self.assertEqual(me.public_orgs, ["ADMINORG"])


class Profile(unittest.TestCase):
    def external_prs(self, viewer, asks):
        api = FakeAPI()  # serves one queued page per ask: the account's own token asks the page twice
        api.org_pages = [[dict(ADMIN), dict(MEMBER)] for _ in range(asks)]
        with mock.patch.object(render, "gql", as_requested(api, viewer)), \
                mock.patch.dict(os.environ, {"GH_WIDGETS_GH_EXTRA_ORGS": ""}):
            user, _days = render.fetch("t", "me")
        return [render.external_counts(user, [pr_to(owner)], []).pr_opened
                for owner in ("AdminOrg", "MemberOrg")]

    def test_work_in_a_member_org_counts_as_external(self):
        self.assertEqual(self.external_prs("me", asks=2), [0, 1])

    def test_another_viewer_never_asks_the_flag_and_keeps_the_visible_orgs(self):
        self.assertEqual(self.external_prs(WORKFLOW_TOKEN, asks=1), [0, 0])

    def test_a_cache_from_before_the_admin_flag_is_refetched(self):
        # v3 org nodes carry no flag, so a fallback render could not tell a member org from an admin one.
        with _ghw_temp.tempdir() as directory:
            path = Path(directory) / "cache.json"
            path.write_text(json.dumps({**complete_cache(), "version": 3}), encoding="utf-8")
            self.assertEqual(render.load_cache(path), {})


class Popularity(unittest.TestCase):
    def test_only_active_admin_memberships_add_repositories(self):
        urls = []
        memberships = [{"role": role, "state": state, "organization": {"login": login}}
                       for login, role, state in (("AdminOrg", "admin", "active"),
                                                  ("MemberOrg", "member", "active"),
                                                  ("PendingOrg", "admin", "pending"))]
        fixtures = {"/user": response(USER),
                    "/user/memberships/orgs?per_page=100": response(memberships),
                    "/user/repos?affiliation=owner&visibility=public&per_page=100": response([])}
        for login in ("AdminOrg", "MemberOrg", "PendingOrg"):
            fixtures[f"/orgs/{login}/repos?type=public&per_page=100"] = response([repo(f"{login}/Repo")])

        def fetch(request, **_kwargs):
            url = request.full_url.removeprefix("https://api.github.com")
            urls.append(url)
            return response({"uniques": 0}) if "/traffic/" in url else fixtures[url]

        with mock.patch.object(popularity.urllib.request, "urlopen", side_effect=fetch), \
                mock.patch.object(popularity, "outsider_commits", return_value=0):
            _user, scores = popularity.fetch_scores("t")
        self.assertEqual([name for name, *_ in scores], ["AdminOrg/Repo"])
        self.assertIn("/orgs/AdminOrg/repos?type=public&per_page=100", urls)
        self.assertFalse([url for url in urls if "MemberOrg" in url or "PendingOrg" in url])


if __name__ == "__main__":
    unittest.main()
