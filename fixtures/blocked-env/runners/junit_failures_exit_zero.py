"""A runner whose report holds a failing case and whose exit code is 0 anyway."""
import pathlib
import sys

REPORT = """<testsuite name="smoke" tests="2" failures="1">
  <testcase classname="smoke.checkout" name="adds_an_item"/>
  <testcase classname="smoke.checkout" name="pays_the_order">
    <failure message="expected 200, got 502"/>
  </testcase>
</testsuite>
"""

pathlib.Path(sys.argv[1]).write_text(REPORT)
