from pathlib import Path

from pytest_pyodide.config import Config, get_global_config

def test_config():
    c = Config()

    c._set_config(Path("dist"), {"chrome"}, False)

    assert c.dist_dir == Path("dist")
    assert c.runtimes == {"chrome"}
    assert c.run_host_test == False

    c._set_config(Path("dist2"), {"firefox"}, True)

    # Should not change

    assert c.dist_dir == Path("dist")
    assert c.runtimes == {"chrome"}
    assert c.run_host_test == False


def test_global_config():
    c = get_global_config()

    c._set_config(Path("dist"), {"chrome"}, False)

    assert c.dist_dir == Path("dist")
    assert c.runtimes == {"chrome"}
    assert c.run_host_test == False

    c2 = get_global_config()

    assert c2.dist_dir == Path("dist")
    assert c2.runtimes == {"chrome"}
    assert c2.run_host_test == False


def test_setters():
    c = Config()

    c.set_chrome_flags(["--headless"])
    assert c.chrome_flags == ["--headless"]

    c.set_firefox_flags(["--headless2"])
    assert c.firefox_flags == ["--headless2"]

    c.set_node_flags(["--expose-gc"])
    assert c.node_flags == ["--expose-gc"]

    c.set_initialize_script("console.log('hello')")
    assert c.initialize_script == "console.log('hello')"