#!/usr/bin/env python3
"""Accessibility metadata and summaries for all rendered SVG cards."""
import importlib.util
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


def load_renderer(module_name, filename):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = load_renderer("accessible_render", "render.py")
impact = load_renderer("accessible_impact", "render-impact.py")
responsiveness = load_renderer(
    "accessible_responsiveness", "render-responsiveness.py")


class CardAccessibility(unittest.TestCase):
    SVG_NS = "http://www.w3.org/2000/svg"

    def assert_accessible_card(self, svg, card, title, desc):
        root = ET.fromstring(svg)
        children = list(root)

        self.assertEqual([child.tag for child in children[:3]], [
            f"{{{self.SVG_NS}}}title",
            f"{{{self.SVG_NS}}}desc",
            f"{{{self.SVG_NS}}}defs",
        ])
        self.assertEqual(root.attrib.get("role"), "img")
        self.assertEqual(root.attrib.get("aria-labelledby"),
                         f"{card}-title {card}-desc")
        self.assertEqual(children[0].attrib.get("id"), f"{card}-title")
        self.assertEqual(children[0].text, title)
        self.assertEqual(children[1].attrib.get("id"), f"{card}-desc")
        self.assertEqual(children[1].text, desc)

    def test_profile_cards_have_accessible_headlines(self):
        colors = render.THEMES["tokyonight"]
        user = {
            "name": "Octo",
            "login": "octo",
            "followers": {"totalCount": 12},
            "repositories": {"totalCount": 4},
        }
        self.assert_accessible_card(
            render.render_stats(colors, user, 5, 6, 1234),
            "stats", "GitHub stats",
            "Followers: 12; public repositories: 4; stars received: 5; "
            "forks received: 6; contributions in the last year: 1,234.")
        self.assert_accessible_card(
            render.render_streak(colors, 12, 31, 1234),
            "streak", "Contribution streak",
            "Current streak: 12 days; longest streak: 31 days; "
            "total contributions: 1,234.")
        self.assert_accessible_card(
            render.render_languages(colors, [
                ("Python", 700, 61.2, "#3572A5"),
                ("Rust", 300, 28.8, "#dea584"),
            ]),
            "languages", "Top languages",
            "Top language: Python (61.2% of code bytes).")
        self.assert_accessible_card(
            render.render_external(colors, 10, 3, 4, 4, 1, 2),
            "external", "External contributions",
            "Pull requests: 10 opened, 3 merged, 4 repos (30% merged); "
            "issues: 4 opened, 1 maintainer-accepted, 2 repos "
            "(25% maintainer-accepted).")

    def test_empty_languages_card_describes_empty_state(self):
        self.assert_accessible_card(
            render.render_languages(render.THEMES["tokyonight"], []),
            "languages", "Top languages", "No language data available.")

    def test_impact_card_has_accessible_headline_figures(self):
        row = impact.ImpactRow(
            score=1.0, base=1.0, share=50.0, ours=250, total=500,
            repo="outside/sample")
        svg = impact.render_impact(
            impact.THEMES["tokyonight"], [row], [row], [row], top_n=1)

        self.assert_accessible_card(
            svg, "impact", "External impact",
            "Shown external repos: 1. Top live-code repo outside/sample: "
            "250 of 500 lines (50.0%).")

    def test_empty_impact_card_describes_empty_state(self):
        svg = impact.render_impact(
            impact.THEMES["tokyonight"], [], [], [])

        self.assert_accessible_card(
            svg, "impact", "External impact",
            "No external contributions to show.")

    def test_impact_without_loc_counts_only_displayed_unique_repos(self):
        def impact_row(repo):
            return impact.ImpactRow(
                score=1.0, base=1.0, share=50.0, ours=250, total=500,
                repo=repo)

        svg = impact.render_impact(
            impact.THEMES["tokyonight"],
            [impact_row("outside/shared"), impact_row("outside/pr-visible"),
             impact_row("outside/pr-hidden")],
            [impact_row("outside/shared"), impact_row("outside/issue-visible")],
            [], top_n=2)

        self.assert_accessible_card(
            svg, "impact", "External impact",
            "Shown external repos: 3. No live-code rows.")

    def test_responsiveness_card_summarizes_prs_and_wait(self):
        svg = responsiveness.render_responsiveness(
            responsiveness.THEMES["tokyonight"], [
                responsiveness.Scored(
                    score=10.0, n=3, hours=2.0, repo="someone/one"),
                responsiveness.Scored(
                    score=8.0, n=6, hours=4.0, repo="someone/two"),
            ])

        self.assert_accessible_card(
            svg, "responsiveness", "External responsiveness",
            "Measured external PRs: 9 across 2 shown repos; median "
            "per-repo average wait: 3.0 h.")

    def test_empty_responsiveness_card_describes_empty_state(self):
        svg = responsiveness.render_responsiveness(
            responsiveness.THEMES["tokyonight"], [])

        self.assert_accessible_card(
            svg, "responsiveness", "External responsiveness",
            "No external PRs yet.")


if __name__ == "__main__":
    unittest.main()
