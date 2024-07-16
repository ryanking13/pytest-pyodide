import pathlib


def test_web_server_secondary(selenium, web_server_secondary):
    host, port, logs = web_server_secondary
    assert pathlib.Path(logs).exists()
    assert selenium.server_port != port


def test_host():
    from pytest_pyodide import get_global_config

    assert (
        get_global_config().run_host_test
    ), "this test should only run when host test is enabled"


def test_runtime(selenium):
    from pytest_pyodide import get_runtimes

    runtimes = get_runtimes()
    assert runtimes, "this test should only run when runtime is specified"


def test_doctest():
    """
    >>> 1+1
    2
    """
    pass
