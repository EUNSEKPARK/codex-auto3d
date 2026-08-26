#!/usr/bin/env python3
"""Run the vendored img2threejs forge test suite against vendor/img2threejs.

The vendored copy is a subset of the upstream repository — the skill and its tooling, not the
project's packaging. A handful of upstream tests assert facts about the *upstream repository*
(its .gitignore, its git index, an optional integration we do not vendor) and can never hold for
a subset, so they are excluded by name with the reason recorded below. Everything else runs, which
is what proves the vendored forge actually works in a fresh checkout.

    python3 tools/vendored_forge_tests.py            # run, honouring the exclusions
    python3 tools/vendored_forge_tests.py --all      # run everything, exclusions included
    python3 tools/vendored_forge_tests.py --list-excluded
"""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from auto3d.util import SKILL_ROOT  # noqa: E402

# test id -> why it cannot apply to a vendored subset
EXCLUDED: dict[str, str] = {
    "test_pipeline.PipelineTest.test_cs2_textures_gitignored_and_never_tracked":
        "reads the upstream repository's .gitignore and git index; vendor/ is not its own repository",
    "test_optional_vision_tooling.OptionalVisionToolingTests.test_optional_environment_declares_all_three_vision_routes":
        "requires integrations/vision/, an optional upstream integration this pipeline does not use",
}


def flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="do not exclude anything")
    parser.add_argument("--list-excluded", action="store_true", help="print the exclusions and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.list_excluded:
        for test_id, reason in EXCLUDED.items():
            print(f"{test_id}\n    {reason}")
        return 0

    tests_dir = SKILL_ROOT / "forge" / "tests"
    if not tests_dir.is_dir():
        print(f"no forge tests at {tests_dir}", file=sys.stderr)
        return 2

    os.chdir(SKILL_ROOT)  # the forge tests resolve fixture paths from the skill root
    discovered = unittest.TestLoader().discover(start_dir=str(tests_dir), pattern="test_*.py", top_level_dir=str(tests_dir))

    suite = unittest.TestSuite()
    skipped = []
    for test in flatten(discovered):
        if not args.all and test.id() in EXCLUDED:
            skipped.append(test.id())
            continue
        suite.addTest(test)

    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)

    if skipped:
        print(f"\nexcluded {len(skipped)} upstream-packaging test(s):")
        for test_id in skipped:
            print(f"  {test_id}\n      {EXCLUDED[test_id]}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
