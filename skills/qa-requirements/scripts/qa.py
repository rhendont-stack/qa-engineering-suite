"""Entry point of the ``qa`` command: ``python3 <dir>/qa.py <command>``, always through an interpreter.

This file keeps to syntax old interpreters parse (no ``from __future__`` import, f-strings, annotations or walrus),
so that they print the version message below instead of a SyntaxError.
"""
import os
import sys


def check_version(info):
    """The message for an interpreter too old to run the kernel, else None."""
    if tuple(info[:2]) < (3, 9):
        return "qa needs Python 3.9 or newer; found %d.%d" % (info[0], info[1])
    return None


if __name__ == "__main__":
    message = check_version(sys.version_info)
    if message is not None:
        print(message)
        sys.exit(2)
    sys.dont_write_bytecode = True
    here = os.path.dirname(os.path.realpath(__file__))
    kernel = os.path.join(here, "_kernel")
    if not os.path.isdir(kernel):
        kernel = os.path.join(os.path.dirname(here), "kernel")
    sys.path.insert(0, kernel)  # first: this file is itself named qa.py and would shadow the package
    import qa.cli
    sys.exit(qa.cli.main())
