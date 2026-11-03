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
        # A class-level guard covers everything in it.
        decorators = node.decorator_list + _enclosing_decorators(tree, node)
        if any(_is_guard(d) for d in decorators):
            continue
        found.append(node.name)
    return found


def _is_guard(decorator):
    """Whether the decorator is a guard, not merely one that names it."""
    name = None
    if isinstance(decorator, ast.Name):
        name = decorator.id
    elif isinstance(decorator, ast.Attribute):
        name = decorator.attr
    return name in ("REQUIRES_BENCH", "REQUIRES_POSIX_SHELL")


def _enclosing_decorators(tree, target):
    """Decorators of the class the function sits directly inside."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if any(child is target for child in node.body):
            return node.decorator_list
    return []


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


def decorator_types(source):
    """Node types of the decorators on the class and on its one method."""
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.ClassDef)
    method = node.body[0]
    assert isinstance(method, ast.FunctionDef)
    return ([type(d) for d in node.decorator_list],
            [type(d) for d in method.decorator_list])


class TestUnguardedTests(unittest.TestCase):
    def verdicts(self, text, node_type):
        """The scan's verdict on one decorator, on a class and on a method."""
        found = []
        for source, types in ((klass(guard=[text]), ([node_type], [])),
                              (klass(own=[text]), ([], [node_type]))):
            self.assertEqual(decorator_types(source), types)
            found.append(unguarded_tests(source))
        return found

    def test_an_undecorated_class_reports_its_method(self):
        self.assertEqual(unguarded_tests(klass()), ["test_probe"])

    def test_a_class_level_guard_covers_its_method(self):
        for guard in GUARDS:
            with self.subTest(guard=guard):
                self.assertEqual(unguarded_tests(klass()), ["test_probe"])
                self.assertEqual(unguarded_tests(klass(guard=[guard])), [])
                self.assertEqual(
                    unguarded_tests(klass(guard=[guard], doc=["Doc."])), [])
                patch = 'mock.patch("x.y", None)'
                for source, types in (
                        (klass(guard=[guard], own=[patch]),
                         ([ast.Name], [ast.Call])),
                        (klass(guard=[patch, guard]), ([ast.Call, ast.Name], [])),
                        (klass(guard=[guard, patch]), ([ast.Name, ast.Call], [])),
                        (klass(own=[patch, guard]), ([], [ast.Call, ast.Name])),
                        (klass(own=[guard, patch]), ([], [ast.Name, ast.Call]))):
                    self.assertEqual(decorator_types(source), types)
                    self.assertEqual(unguarded_tests(source), [])

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
                klass_node = ast.parse(doc).body[0]
                assert isinstance(klass_node, ast.ClassDef)
                self.assertEqual(ast.get_docstring(klass_node), f"Wants {guard}.")
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
                self.assertEqual(len(ast.parse(outer_guarded).body), 1)
                outer_bare = klass(nested=[guarded])
                self.assertEqual(unguarded_tests(outer_guarded), [inner])
                self.assertEqual(unguarded_tests(outer_bare), ["test_probe"])

    def test_a_decorator_that_only_names_a_guard_is_not_one(self):
        for guard in GUARDS:
            for text in (f'mock.patch("x.{guard}", None)',
                         f"unittest.skipUnless({guard}, 'why')",
                         f"mock.patch.object(bench.{guard}, 'x')",
                         f"{guard}()"):
                with self.subTest(guard=guard, text=text):
                    self.assertEqual(self.verdicts(text, ast.Call),
                                     [["test_probe"]] * 2)

    def test_a_guard_is_a_name_or_an_attribute_ending_in_one(self):
        for guard in GUARDS:
            for text, node_type in ((guard, ast.Name),
                                    (f"bench_platform.{guard}", ast.Attribute),
                                    (f"scripts.bench.{guard}", ast.Attribute)):
                with self.subTest(guard=guard, text=text):
                    self.assertEqual(unguarded_tests(klass()), ["test_probe"])
                    self.assertEqual(self.verdicts(text, node_type), [[], []])

    def test_a_name_that_only_contains_a_guard_is_not_one(self):
        for guard in GUARDS:
            for text, node_type in ((f"{guard}_X", ast.Name),
                                    (f"NOT_{guard}", ast.Name),
                                    (f"bench.{guard}_X", ast.Attribute),
                                    (f"bench.NOT_{guard}", ast.Attribute),
                                    (f"{guard}.skip", ast.Attribute)):
                with self.subTest(guard=guard, text=text):
                    self.assertEqual(self.verdicts(text, node_type),
                                     [["test_probe"]] * 2)

    def test_a_function_outside_any_class_is_judged_by_its_own_decorators(self):
        for guard in GUARDS:
            with self.subTest(guard=guard):
                bare = f"def test_probe():\n    {MEASURE}\n"
                guarded = f"@{guard}\n{bare}"
                function = ast.parse(guarded).body[0]
                assert isinstance(function, ast.FunctionDef)
                self.assertEqual([type(d) for d in function.decorator_list],
                                 [ast.Name])
                self.assertEqual(unguarded_tests(bare), ["test_probe"])
                self.assertEqual(unguarded_tests(guarded), [])
