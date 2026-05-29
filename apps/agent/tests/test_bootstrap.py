"""Bootstrap smoke — proves CI runs at all. Replaced by real tests in PR-2+."""


def test_python_version_ok():
    import sys
    assert sys.version_info >= (3, 12)


def test_package_imports():
    import agent  # noqa: F401
