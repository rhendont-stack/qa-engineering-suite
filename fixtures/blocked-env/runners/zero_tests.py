"""A runner that finds no tests, says so in its report, and exits 0 anyway."""
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_text('<testsuite name="smoke" tests="0"></testsuite>\n')
