"""Only orgs the account administers are its own: a plain membership feeds no card."""
import os
import unittest
from unittest import mock

from tests.test_popularity import USER, popularity, repo, response
from tests.test_render import FakeAPI, render

common = render.common
FIELD = "viewerCanAdminister"
ADMIN = {"login": "AdminOrg", FIELD: True}
MEMBER = {"login": "MemberOrg", FIELD: False}


def as_requested(answer):
    """GitHub answers only the fields a query names, so drop the admin flag unless asked."""
    def gql(token, query, variables=None, **kwargs):
        data = answer(token, query, variables, **kwargs)
        if FIELD not in query:
            for node in ((data.get("user") or {}).get("organizations") or {}).get("nodes", []):
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
        legacy = {"login": "UnflaggedOrg"}
        me = common.fetch_identity("t", "octocat", include_environment=False,
                                   gql_fn=as_requested(identity_answer([ADMIN, MEMBER, legacy])))
        self.assertEqual(me.orgs, ["AdminOrg"])
        self.assertEqual(me.insiders, {"octocat", "adminorg"})

    def test_the_public_snapshot_view_keeps_only_administered_public_orgs(self):
        nodes = [{**ADMIN, "isPublic": True}, {**MEMBER, "isPublic": True}]
        me = common.fetch_identity("t", "octocat", include_environment=False, include_public_orgs=True,
                                   gql_fn=as_requested(identity_answer(nodes)))
        self.assertEqual(me.public_orgs, ["AdminOrg"])
        # The production path reads public memberships unauthenticated, which knows no roles.
        with mock.patch.object(common, "gql", side_effect=as_requested(identity_answer([ADMIN, MEMBER]))), \
                mock.patch.object(common, "fetch_public_organizations", return_value=["ADMINORG", "MemberOrg"]):
            me = common.fetch_identity("t", "octocat", include_environment=False, include_public_orgs=True)
        self.assertEqual(me.public_orgs, ["ADMINORG"])


class Profile(unittest.TestCase):
    def test_work_in_a_member_org_counts_as_external(self):
        api = FakeAPI()
        api.org_pages = [[dict(ADMIN), dict(MEMBER)]]
        with mock.patch.object(render, "gql", as_requested(api)), \
                mock.patch.dict(os.environ, {"GH_WIDGETS_GH_EXTRA_ORGS": ""}):
            user, _days = render.fetch("t", "me")
        self.assertEqual(render.external_counts(user, [pr_to("MemberOrg")], []).pr_opened, 1)
        self.assertEqual(render.external_counts(user, [pr_to("AdminOrg")], []).pr_opened, 0)


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
