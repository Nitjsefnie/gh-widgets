# Agent instructions

Look around and follow the existing style before implementing. Before you
write a renderer, a card layout, a helper, a test or a README section, find
how this repository already does the same thing and reuse that code or match
that style. A new card looks like the existing cards, and a new section reads
like the sections around it.

Runtime code lives in `src/`, the unittest package in `tests/`, gate data in
`gate/`, and pinned toolchains in `requirements/`. The public
`ghwidgets_data.py` stays at the root for pinned consumers. Run tests from
the root with `python3 -m unittest discover -v` or select a module with
`python3 -m unittest tests.test_render`.

The standing rules are in `CLAUDE.md` and `CONTRIBUTING.md`.
