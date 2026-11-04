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
KINDS = {"def": ast.FunctionDef, "async def": ast.AsyncFunctionDef}


def unguarded_tests(source):
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
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
          nested=(), kind="def"):
    return "".join([
        *(f"@{name}\n" for name in guard),
        "class TestSample(unittest.TestCase):\n",
        *(f'    """{text}"""\n' for text in doc),
        *(f"    @{name}\n" for name in own),
        f"    {kind} {method}(self):\n",
        f"        {call}\n",
        *(textwrap.indent(source, "    ") for source in nested)])


def decorator_types(source):
    """Node types of the decorators on the class and on its one method."""
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.ClassDef)
    method = node.body[0]
    assert isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
    return ([type(d) for d in node.decorator_list],
            [type(d) for d in method.decorator_list])


def member(keyword, name, own=()):
    """One more method for klass() to nest in its class."""
    return "".join([*(f"@{text}\n" for text in own),
                    f"{keyword} {name}(self):\n",
                    f"    {MEASURE}\n"])


def definition(source):
    """The one function, or the one method of the one class, in source."""
    node = ast.parse(source).body[0]
    if isinstance(node, ast.ClassDef):
        node = next(n for n in node.body if not isinstance(n, ast.Expr))
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    return node


def body_shape(source):
    """Node type, name and decorator node types of each member of the class."""
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.ClassDef)
    shape = []
    for child in node.body:
        assert isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        shape.append((type(child), child.name,
                      [type(d) for d in child.decorator_list]))
    return shape


class TestUnguardedTests(unittest.TestCase):
    def assert_defines(self, source, keyword):
        self.assertIs(type(definition(source)), KINDS[keyword])

    def verdicts(self, text, node_type, keyword):
        """The scan's verdict on one decorator, on a class and on a method."""
        found = []
        for source, types in (
                (klass(guard=[text], kind=keyword), ([node_type], [])),
                (klass(own=[text], kind=keyword), ([], [node_type]))):
            self.assert_defines(source, keyword)
            self.assertEqual(decorator_types(source), types)
            found.append(unguarded_tests(source))
        return found

    def test_an_undecorated_class_reports_its_method(self):
        for keyword in KINDS:
            with self.subTest(keyword=keyword):
                source = klass(kind=keyword)
                self.assert_defines(source, keyword)
                self.assertEqual(unguarded_tests(source), ["test_probe"])

    def test_a_class_level_guard_covers_its_method(self):
        for guard in GUARDS:
            for keyword in KINDS:
                with self.subTest(guard=guard, keyword=keyword):
                    self.assertEqual(unguarded_tests(klass(kind=keyword)),
                                     ["test_probe"])
                    doc = klass(guard=[guard], doc=["Doc."], kind=keyword)
                    self.assert_defines(doc, keyword)
                    self.assertEqual(unguarded_tests(doc), [])
                    patch = 'mock.patch("x.y", None)'
                    for source, types in (
                            (klass(guard=[guard], kind=keyword),
                             ([ast.Name], [])),
                            (klass(guard=[guard], own=[patch], kind=keyword),
                             ([ast.Name], [ast.Call])),
                            (klass(guard=[patch, guard], kind=keyword),
                             ([ast.Call, ast.Name], [])),
                            (klass(guard=[guard, patch], kind=keyword),
                             ([ast.Name, ast.Call], [])),
                            (klass(own=[patch, guard], kind=keyword),
                             ([], [ast.Call, ast.Name])),
                            (klass(own=[guard, patch], kind=keyword),
                             ([], [ast.Name, ast.Call]))):
                        self.assert_defines(source, keyword)
                        self.assertEqual(decorator_types(source), types)
                        self.assertEqual(unguarded_tests(source), [])

    def test_a_method_level_guard_covers_its_method(self):
        for guard in GUARDS:
            for keyword in KINDS:
                with self.subTest(guard=guard, keyword=keyword):
                    self.assertEqual(unguarded_tests(klass(kind=keyword)),
                                     ["test_probe"])
                    source = klass(own=[guard], kind=keyword)
                    self.assert_defines(source, keyword)
                    self.assertEqual(decorator_types(source), ([], [ast.Name]))
                    self.assertEqual(unguarded_tests(source), [])

    def test_each_way_of_reaching_the_instrument_is_found(self):
        reaches = (("run(cmd)", []),
                   (RUNNER, ["test_probe"]),
                   (CLI, ["test_probe"]))
        awaits = ((f"await {MEASURE}", ["test_probe"]),
                  (f"await {RUNNER}", ["test_probe"]),
                  (f"await {CLI}", ["test_probe"]),
                  (f"async with {MEASURE}: pass", ["test_probe"]),
                  ("await run(cmd)", []))
        for keyword, node_type, calls in (
                ("def", ast.FunctionDef, reaches),
                ("async def", ast.AsyncFunctionDef, reaches + awaits)):
            with self.subTest(keyword=keyword):
                self.assertIs(KINDS[keyword], node_type)
            for call, found in calls:
                with self.subTest(keyword=keyword, call=call):
                    source = klass(call=call, kind=keyword)
                    # ast.parse takes an await in a plain def; compile does not.
                    compile(source, "<fixture>", "exec")
                    self.assertIs(type(definition(source)), node_type)
                    self.assertEqual(unguarded_tests(source), found)
            with self.subTest(keyword=keyword, method="probe"):
                source = klass(method="probe", kind=keyword)
                self.assertIs(type(definition(source)), node_type)
                self.assertEqual(unguarded_tests(source), [])

    def test_naming_a_guard_in_the_class_is_not_declaring_one(self):
        for guard in GUARDS:
            for keyword in KINDS:
                with self.subTest(guard=guard, keyword=keyword):
                    doc = klass(doc=[f"Wants {guard}."], kind=keyword)
                    self.assert_defines(doc, keyword)
                    klass_node = ast.parse(doc).body[0]
                    assert isinstance(klass_node, ast.ClassDef)
                    self.assertEqual(ast.get_docstring(klass_node),
                                     f"Wants {guard}.")
                    body = klass(call=f"{MEASURE}; _ = '{guard}'", kind=keyword)
                    self.assert_defines(body, keyword)
                    self.assertEqual(unguarded_tests(doc), ["test_probe"])
                    self.assertEqual(unguarded_tests(body), ["test_probe"])

    def test_a_guard_on_one_class_does_not_cover_another(self):
        for guard in GUARDS:
            for keyword in KINDS:
                with self.subTest(guard=guard, keyword=keyword):
                    guarded = klass(guard=[guard], kind=keyword)
                    bare = klass(kind=keyword)
                    self.assert_defines(guarded, keyword)
                    self.assert_defines(bare, keyword)
                    self.assertEqual(unguarded_tests(guarded), [])
                    self.assertEqual(unguarded_tests(bare), ["test_probe"])
                    for source in (guarded + bare, bare + guarded):
                        self.assertEqual(unguarded_tests(source),
                                         ["test_probe"])

    def test_a_nested_class_is_judged_by_its_own_guards(self):
        for guard in GUARDS:
            for keyword in KINDS:
                with self.subTest(guard=guard, keyword=keyword):
                    inner = "test_inner"
                    bare = klass(method=inner, kind=keyword)
                    self.assert_defines(bare, keyword)
                    guarded = klass(guard=[guard], method=inner, kind=keyword)
                    self.assert_defines(guarded, keyword)
                    self.assertEqual(unguarded_tests(bare), [inner])
                    self.assertEqual(unguarded_tests(guarded), [])
                    outer_guarded = klass(guard=[guard], nested=[bare],
                                          kind=keyword)
                    self.assertEqual(len(ast.parse(outer_guarded).body), 1)
                    self.assert_defines(outer_guarded, keyword)
                    outer_bare = klass(nested=[guarded], kind=keyword)
                    self.assert_defines(outer_bare, keyword)
                    self.assertEqual(unguarded_tests(outer_guarded), [inner])
                    self.assertEqual(unguarded_tests(outer_bare),
                                     ["test_probe"])

    def test_a_decorator_that_only_names_a_guard_is_not_one(self):
        for guard in GUARDS:
            for keyword in KINDS:
                for text in (f'mock.patch("x.{guard}", None)',
                             f"unittest.skipUnless({guard}, 'why')",
                             f"mock.patch.object(bench.{guard}, 'x')",
                             f"{guard}()"):
                    with self.subTest(guard=guard, keyword=keyword, text=text):
                        self.assertEqual(
                            self.verdicts(text, ast.Call, keyword),
                            [["test_probe"]] * 2)

    def test_a_guard_is_a_name_or_an_attribute_ending_in_one(self):
        for guard in GUARDS:
            for keyword in KINDS:
                for text, node_type in (
                        (guard, ast.Name),
                        (f"bench_platform.{guard}", ast.Attribute),
                        (f"scripts.bench.{guard}", ast.Attribute)):
                    with self.subTest(guard=guard, keyword=keyword, text=text):
                        self.assertEqual(unguarded_tests(klass(kind=keyword)),
                                         ["test_probe"])
                        self.assertEqual(
                            self.verdicts(text, node_type, keyword), [[], []])

    def test_a_name_that_only_contains_a_guard_is_not_one(self):
        for guard in GUARDS:
            for keyword in KINDS:
                for text, node_type in ((f"{guard}_X", ast.Name),
                                        (f"NOT_{guard}", ast.Name),
                                        (f"bench.{guard}_X", ast.Attribute),
                                        (f"bench.NOT_{guard}", ast.Attribute),
                                        (f"{guard}.skip", ast.Attribute)):
                    with self.subTest(guard=guard, keyword=keyword, text=text):
                        self.assertEqual(
                            self.verdicts(text, node_type, keyword),
                            [["test_probe"]] * 2)

    def test_a_function_outside_any_class_is_judged_by_its_own_decorators(self):
        for guard in GUARDS:
            for keyword in KINDS:
                bare = f"{keyword} test_probe():\n    {MEASURE}\n"
                for text, types, found in (
                        ("", [], ["test_probe"]),
                        (f"@{guard}\n", [ast.Name], []),
                        (f'@mock.patch("x.{guard}", None)\n', [ast.Call],
                         ["test_probe"])):
                    with self.subTest(guard=guard, keyword=keyword,
                                      decorator=text):
                        source = text + bare
                        self.assert_defines(source, keyword)
                        self.assertEqual(
                            [type(d) for d in definition(source).decorator_list],
                            types)
                        self.assertEqual(unguarded_tests(source), found)

    def test_sync_and_async_methods_of_one_class_are_judged_independently(self):
        for guard in GUARDS:
            with self.subTest(guard=guard):
                patch = f'mock.patch("x.{guard}", None)'
                members = ["async def test_async_unreached(self):\n    run(cmd)\n",
                           member("async def", "test_async_bare"),
                           member("def", "test_sync_guarded", own=[guard]),
                           member("async def", "test_async_patched",
                                  own=[patch]),
                           member("async def", "test_async_guarded",
                                  own=[guard]),
                           member("def", "test_sync_patched", own=[patch]),
                           member("async def", "helper_async"),
                           member("def", "helper_sync")]
                bare = klass(method="test_sync_bare", nested=members)
                covered = klass(guard=[guard], method="test_sync_bare",
                                nested=members)
                for source in (bare, covered):
                    self.assertEqual(
                        body_shape(source),
                        [(ast.FunctionDef, "test_sync_bare", []),
                         (ast.AsyncFunctionDef, "test_async_unreached", []),
                         (ast.AsyncFunctionDef, "test_async_bare", []),
                         (ast.FunctionDef, "test_sync_guarded", [ast.Name]),
                         (ast.AsyncFunctionDef, "test_async_patched",
                          [ast.Call]),
                         (ast.AsyncFunctionDef, "test_async_guarded",
                          [ast.Name]),
                         (ast.FunctionDef, "test_sync_patched", [ast.Call]),
                         (ast.AsyncFunctionDef, "helper_async", []),
                         (ast.FunctionDef, "helper_sync", [])])
                self.assertEqual(decorator_types(bare)[0], [])
                self.assertEqual(decorator_types(covered)[0], [ast.Name])
                reported = ["test_sync_bare", "test_async_bare",
                            "test_async_patched", "test_sync_patched"]
                self.assertEqual(unguarded_tests(bare), reported)
                self.assertEqual(unguarded_tests(bare + bare), reported * 2)
                self.assertEqual(unguarded_tests(covered), [])
