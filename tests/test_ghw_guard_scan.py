"""The unguarded-instrument scan reads the guards on a method and its class."""
import ast
import textwrap
import unittest

# Reaches, spelled the two ways the instrument is actually entered.
REACHES = (".measure(", "counter.py")
GUARDS = ("REQUIRES_BENCH", "REQUIRES_POSIX_SHELL")
MEASURE = "counter.measure(cmd)"
RUNNER = "runner.measure(cmd)"
CLI = "run(['scripts/ci/counter.py'])"


def unguarded_tests(source):
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        body = ast.get_source_segment(source, node) or ""
        if not any(reach in body for reach in REACHES):
            continue
        decorators = "".join(
            ast.get_source_segment(source, d) or ""
            for d in node.decorator_list)
        # A class-level guard covers everything in it.
        enclosing = _enclosing_guards(source, tree, node)
        if any(word in decorators + enclosing
               for word in ("REQUIRES_BENCH", "REQUIRES_POSIX_SHELL")):
            continue
        found.append(node.name)
    return found


def _enclosing_guards(source, tree, target):
    """Guards declared on a class the function sits inside."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if any(child is target for child in node.body):
            return "".join(ast.get_source_segment(source, d) or ""
                           for d in node.decorator_list)
    return ""


def klass(guard=(), own=(), method="test_probe", call=MEASURE, doc=(),
          nested=()):
    return "".join([
        *(f"@{name}\n" for name in guard),
        "class TestSample(unittest.TestCase):\n",
        *(f'    """{text}"""\n' for text in doc),
        *(f"    @{name}\n" for name in own),
        f"    def {method}(self):\n",
        f"        {call}\n",
        *(textwrap.indent(source, "    ") for source in nested)])


class TestUnguardedTests(unittest.TestCase):
    def test_an_undecorated_class_reports_its_method(self):
        self.assertEqual(unguarded_tests(klass()), ["test_probe"])

    def test_a_class_level_guard_covers_its_method(self):
        for guard in GUARDS:
            with self.subTest(guard=guard):
                self.assertEqual(unguarded_tests(klass()), ["test_probe"])
                self.assertEqual(unguarded_tests(klass(guard=[guard])), [])
                self.assertEqual(
                    unguarded_tests(klass(guard=[guard], doc=["Doc."])), [])

    def test_a_method_level_guard_covers_its_method(self):
        for guard in GUARDS:
            with self.subTest(guard=guard):
                self.assertEqual(unguarded_tests(klass()), ["test_probe"])
                self.assertEqual(unguarded_tests(klass(own=[guard])), [])

    def test_each_way_of_reaching_the_instrument_is_found(self):
        self.assertEqual(unguarded_tests(klass(call="run(cmd)")), [])
        self.assertEqual(unguarded_tests(klass(call=RUNNER)), ["test_probe"])
        self.assertEqual(unguarded_tests(klass(call=CLI)), ["test_probe"])
        self.assertEqual(unguarded_tests(klass(method="probe")), [])

    def test_naming_a_guard_in_the_class_is_not_declaring_one(self):
        for guard in GUARDS:
            with self.subTest(guard=guard):
                doc = klass(doc=[f"Wants {guard}."])
                body = klass(call=f"{MEASURE}; _ = '{guard}'")
                self.assertEqual(unguarded_tests(doc), ["test_probe"])
                self.assertEqual(unguarded_tests(body), ["test_probe"])

    def test_a_guard_on_one_class_does_not_cover_another(self):
        for guard in GUARDS:
            with self.subTest(guard=guard):
                guarded = klass(guard=[guard])
                bare = klass()
                self.assertEqual(unguarded_tests(guarded), [])
                self.assertEqual(unguarded_tests(bare), ["test_probe"])
                for source in (guarded + bare, bare + guarded):
                    self.assertEqual(unguarded_tests(source), ["test_probe"])

    def test_a_nested_class_is_judged_by_its_own_guards(self):
        for guard in GUARDS:
            with self.subTest(guard=guard):
                inner = "test_inner"
                bare = klass(method=inner)
                guarded = klass(guard=[guard], method=inner)
                self.assertEqual(unguarded_tests(bare), [inner])
                self.assertEqual(unguarded_tests(guarded), [])
                outer_guarded = klass(guard=[guard], nested=[bare])
                outer_bare = klass(nested=[guarded])
                self.assertEqual(unguarded_tests(outer_guarded), [inner])
                self.assertEqual(unguarded_tests(outer_bare), ["test_probe"])
