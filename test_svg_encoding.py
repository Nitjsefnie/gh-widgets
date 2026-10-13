#!/usr/bin/env python3
"""Tests for one ghwidgets_common.py helper. Stdlib only, like the module.

    python3 -m unittest discover -v

No network; no filesystem outside a TemporaryDirectory.
"""
import importlib.util
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "ghwidgets_common", Path(__file__).with_name("ghwidgets_common.py"))
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load ghwidgets_common.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)

stars_spec = importlib.util.spec_from_file_location(
    "encoding_stars", Path(__file__).with_name("render-stars.py"))
if stars_spec is None or stars_spec.loader is None:
    raise SystemExit("error: cannot load render-stars.py")
stars = importlib.util.module_from_spec(stars_spec)
stars_spec.loader.exec_module(stars)


class StarSvgEncoding(unittest.TestCase):
    def test_generated_svg_is_ascii_and_controls_are_stripped(self):
        svg = stars.render_stars(common.THEMES["tokyonight"],
                                 'owner/repo\x01\x1b&<"',
                                 {"total": 1, "starred_at": ["2026-09-04T00:00:00Z"]})
        svg.encode("ascii")
        ET.fromstring(svg)
        self.assertNotIn("\x01", svg)
        self.assertNotIn("\x1b", svg)
        self.assertIn("&amp;&lt;&quot;", svg)


class XmlEscapeControlStripping(unittest.TestCase):
    def test_xml_forbidden_c0_controls_are_removed(self):
        forbidden = "".join(chr(code) for code in
                            list(range(0x00, 0x09)) + [0x0b, 0x0c] +
                            list(range(0x0e, 0x20)))
        self.assertEqual(common.xml_escape("a\x01b\x1bc"), "abc")
        self.assertEqual(common.xml_escape(forbidden), "")

    def test_xml_legal_whitespace_controls_survive(self):
        self.assertEqual(common.xml_escape("a\tb\nc\rd"), "a\tb\nc\rd")


class XmlColor(unittest.TestCase):
    def test_six_and_eight_digit_hex_colors_are_preserved(self):
        for value in ("#Ab12eF", "#Ab12eF80"):
            with self.subTest(value=value):
                self.assertEqual(common.xml_color(value), value)

    def test_invalid_colors_use_the_fallback(self):
        for value in ("red", "#zz1211", '\"><img src=x>', ""):
            with self.subTest(value=value):
                self.assertEqual(common.xml_color(value), "#888888")
