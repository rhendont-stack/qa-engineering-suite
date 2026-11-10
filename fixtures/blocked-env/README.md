# blocked-env

A project whose test commands cannot give a plain answer. Each one is run through `qa run`.

Run the project's tests with:

    qa-absent-runner --suite smoke

`qa-absent-runner` is a test runner that is not installed anywhere this fixture is meant to run.

Two other runners ship with the project. Each takes the path of the JUnit report to write:

    python3 runners/zero_tests.py <report.xml>
    python3 runners/junit_failures_exit_zero.py <report.xml>
