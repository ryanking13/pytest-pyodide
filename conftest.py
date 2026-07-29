pytest_plugins = [
    "pytester",
]

from pytest_pyodide import get_global_config


def set_configs():
    pytest_pyodide_config = get_global_config()

    pytest_pyodide_config.add_node_extra_globals(["Request", "Response"])


set_configs()
