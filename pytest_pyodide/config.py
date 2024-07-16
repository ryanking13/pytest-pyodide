"""
Stores the global runtime configuration related to the pytest_pyodide package.
"""
from pathlib import Path


class Config:
    def __init__(self):
        # The directory pointing to the Pyodide distribution, passed as a `--dist-dir` argument.
        self.dist_dir: Path = Path()
        # Runtimes to be used for running tests. Corresponds to the `--runtime` argument.
        self.runtimes: set[str] = set()
        # Whether to run the host tests.
        self.run_host_test: bool = True
        
        self._set = False

        # Flags to be passed to the Chrome browser.
        self.chrome_flags: list[str] = ["--js-flags=--expose-gc"]
        # Flags to be passed to the Firefox browser.
        self.firefox_flags: list[str] = []
        # Flags to be passed to the Node.js runtime.
        self.node_flags: list[str] = []

        # The script to be executed to initialize the runtime.
        self.initialize_script: str = "pyodide.runPython('');"

    def _set_config(self, dist_dir: Path, runtimes: set[str], run_host_test: bool):
        """
        Set the configuration values passed from the command line.
        This method should not be used outside ot this package.
        """
        if self._set:
            # set_config is expected to be called only once inside pytest_configure().
            # However, when `pytester` is used it calls pytest_configure() mutliple times, resulting in overwriting the configuration.
            # To prevent this, we ignore the subsequent calls to set_config().
            return None

        self._set = True
        self.dist_dir = dist_dir
        self.runtimes = runtimes
        self.run_host_test = run_host_test

    def set_chrome_flags(self, chrome_flags: list[str]):
        self.chrome_flags = chrome_flags
    
    def set_firefox_flags(self, firefox_flags: list[str]):
        self.firefox_flags = firefox_flags

    def set_node_flags(self, node_flags: list[str]):
        self.node_flags = node_flags
    
    def set_initialize_script(self, initialize_script: str):
        self.initialize_script = initialize_script


SINGLETON = Config()

def get_global_config() -> Config:
    """
    Return the singleton config object.
    """
    return SINGLETON


def get_dist_dir() -> Path:
    return get_global_config().dist_dir


def get_runtimes() -> set[str]:
    return get_global_config().runtimes