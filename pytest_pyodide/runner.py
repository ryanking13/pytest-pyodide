import json
import logging
import os
import tempfile
import textwrap
import time
from pathlib import Path

import pexpect
import pytest

from .config import RUNTIMES, get_global_config

logger = logging.getLogger(__name__)

TEST_SETUP_CODE = """
Error.stackTraceLimit = Infinity;

// Fix globalThis is messed up in firefox see facebook/react#16606.
// Replace it with window.
globalThis.globalThis = globalThis.window || globalThis;

globalThis.sleep = function (s) {
    return new Promise((resolve) => setTimeout(resolve, s));
};

globalThis.assert = function (cb, message = "") {
    if (message !== "") {
        message = "\\n" + message;
    }
    if (cb() !== true) {
        throw new Error(
            `Assertion failed: ${cb.toString().slice(6)}${message}`
        );
    }
};

globalThis.assertAsync = async function (cb, message = "") {
    if (message !== "") {
        message = "\\n" + message;
    }
    if ((await cb()) !== true) {
        throw new Error(
            `Assertion failed: ${cb.toString().slice(12)}${message}`
        );
    }
};

function checkError(err, errname, pattern, pat_str, thiscallstr) {
    if (typeof pattern === "string") {
        pattern = new RegExp(pattern);
    }
    if (!err) {
        throw new Error(`${thiscallstr} failed, no error thrown`);
    }
    if (err.constructor.name !== errname) {
        throw new Error(
            `${thiscallstr} failed, expected error ` +
                `of type '${errname}' got type '${err.constructor.name}'`
        );
    }
    if (!pattern.test(err.message)) {
        throw new Error(
            `${thiscallstr} failed, expected error ` +
                `message to match pattern ${pat_str} got:\n${err.message}`
        );
    }
}

globalThis.assertThrows = function (cb, errname, pattern) {
    let pat_str = typeof pattern === "string" ? `"${pattern}"` : `${pattern}`;
    let thiscallstr = `assertThrows(${cb.toString()}, "${errname}", ${pat_str})`;
    let err = undefined;
    try {
        cb();
    } catch (e) {
        err = e;
    }
    checkError(err, errname, pattern, pat_str, thiscallstr);
};

globalThis.assertThrowsAsync = async function (cb, errname, pattern) {
    let pat_str = typeof pattern === "string" ? `"${pattern}"` : `${pattern}`;
    let thiscallstr = `assertThrowsAsync(${cb.toString()}, "${errname}", ${pat_str})`;
    let err = undefined;
    try {
        await cb();
    } catch (e) {
        err = e;
    }
    checkError(err, errname, pattern, pat_str, thiscallstr);
};
""".strip()


class JavascriptException(Exception):
    def __init__(self, msg, stack):
        self.msg = msg
        self.stack = stack
        # In chrome the stack contains the message
        if self.stack and self.stack.startswith(self.msg):
            self.msg = ""

    def __str__(self):
        return "\n\n".join(x for x in [self.msg, self.stack] if x)


class _BrowserBaseRunner:
    browser: RUNTIMES = ""  # type: ignore[assignment]
    runner: str = ""
    script_timeout = 20
    JavascriptException = JavascriptException

    # A common script that runs after pyodide is loaded
    POST_LOAD_PYODIDE_SCRIPT = """
    self.pyodide = pyodide;
    globalThis.pyodide = pyodide;
    pyodide._api.inTestHoist = true; // improve some error messages for tests
    """

    def __init__(
        self,
        server_port,
        server_hostname="127.0.0.1",
        server_log=None,
        load_pyodide=True,
        dist_dir=None,
        jspi=False,
        **kwargs,
    ):
        self._config = get_global_config()

        self.server_port = server_port
        self.server_hostname = server_hostname
        self.base_url = f"http://{self.server_hostname}:{self.server_port}"
        self.server_log = server_log
        self.dist_dir = dist_dir
        self.jspi = jspi
        self.driver = self.get_driver(jspi)

        self.set_script_timeout(self.script_timeout)
        self.prepare_driver()
        self.javascript_setup()
        if load_pyodide:
            self.load_pyodide()
            self.initialize_pyodide()
            self.save_state()
            self.restore_state()

    def get_driver(self, jspi=False):
        raise NotImplementedError()

    def goto(self, page):
        raise NotImplementedError()

    def set_script_timeout(self, timeout):
        raise NotImplementedError()

    def quit(self):
        raise NotImplementedError()

    def refresh(self):
        raise NotImplementedError()

    def run_js_inner(self, code, check_code):
        raise NotImplementedError()

    def prepare_driver(self):
        self.goto(f"{self.base_url}/module_test.html")

    def javascript_setup(self):
        self.run_js(
            TEST_SETUP_CODE,
            pyodide_checks=False,
        )

    def load_pyodide(self):
        self.run_js(
            self._config.get_load_pyodide_script(self.browser)
            + self.POST_LOAD_PYODIDE_SCRIPT
        )

    def initialize_pyodide(self):
        self.run_js("""
            let isPyProxy;
            if(pyodide.ffi) {
                isPyProxy = (o) => o instanceof pyodide.ffi.PyProxy;
            } else {
                isPyProxy = pyodide.isPyProxy;
            }
            pyodide.$handleTestResult = function(result) {
                if(!(result && result.toJs)){
                    return result;
                }
                let converted_result = result.toJs();
                if(isPyProxy(converted_result)){
                    converted_result = undefined;
                }
                result.destroy();
                return converted_result;
            }
            """)
        self.run_js(self._config.get_initialize_script())
        from .decorator import initialize_decorator

        initialize_decorator(self)

    @property
    def pyodide_loaded(self):
        return self.run_js("return !!(self.pyodide && self.pyodide.runPython);")

    @property
    def logs(self):
        logs = self.run_js("return self.logs;", pyodide_checks=False)
        if logs is not None:
            return "\n".join(str(x) for x in logs)
        return ""

    def clean_logs(self):
        self.run_js("self.logs = []", pyodide_checks=False)

    def run(self, code):
        return self.run_js(f"""
            let result = pyodide.runPython({code!r});
            return pyodide.$handleTestResult(result);
            """)

    def run_async(self, code):
        return self.run_js(f"""
            await pyodide.loadPackagesFromImports({code!r})
            let result = await pyodide.runPythonAsync({code!r});
            return pyodide.$handleTestResult(result);
            """)

    def run_js(self, code, pyodide_checks=True):
        """Run JavaScript code and check for pyodide errors"""
        if isinstance(code, str) and code.startswith("\n"):
            # we have a multiline string, fix indentation
            code = textwrap.dedent(code)

        if pyodide_checks:
            check_code = """
                    if(globalThis.pyodide && pyodide._module && pyodide._module._PyErr_Occurred()){
                        try {
                            pyodide._module._pythonexc2js();
                        } catch(e){
                            console.error(`Python exited with error flag set! Error was:\n${e.message}`);
                            // Don't put original error message in new one: we want
                            // "pytest.raises(xxx, match=msg)" to fail
                            throw new Error(`Python exited with error flag set!`);
                        }
                    }
           """
        else:
            check_code = ""
        return self.run_js_inner(code, check_code)

    def get_num_hiwire_keys(self):
        return self.run_js("return pyodide._module.hiwire.num_keys();")

    @property
    def force_test_fail(self) -> bool:
        return self.run_js("return !!pyodide._api.fail_test;")  # type: ignore[no-any-return]

    def clear_force_test_fail(self):
        self.run_js("pyodide._api.fail_test = false;")

    def save_state(self):
        self.run_js("self.__savedState = pyodide._api.saveState();")

    def restore_state(self):
        self.run_js("""
            if(self.__savedState){
                pyodide._api.restoreState(self.__savedState)
            }
            """)

    def get_num_proxies(self):
        return self.run_js("return pyodide._module.pyproxy_alloc_map.size")

    def enable_pyproxy_tracing(self):
        self.run_js("pyodide._module.enable_pyproxy_allocation_tracing()")

    def disable_pyproxy_tracing(self):
        self.run_js("pyodide._module.disable_pyproxy_allocation_tracing()")

    def run_webworker(self, code):
        if isinstance(code, str) and code.startswith("\n"):
            # we have a multiline string, fix indentation
            code = textwrap.dedent(code)

        worker_file = "module_webworker_dev.js"

        return self.run_js(
            """
            let worker = new Worker('{}', {{ type: 'module' }});
            let res = new Promise((res, rej) => {{
                worker.onerror = e => rej(e);
                worker.onmessage = e => {{
                    if (e.data.results) {{
                       res(e.data.results);
                    }} else {{
                       rej(e.data.error);
                    }}
                }};
                worker.postMessage({{ python: {!r} }});
            }});
            return await res
            """.format(
                f"http://{self.server_hostname}:{self.server_port}/{worker_file}",
                code,
            ),
            pyodide_checks=False,
        )

    def load_package(self, packages):
        # Pyodide's ``loadPackage`` reports failures in two different ways:
        #
        #   * For a known-but-unresolvable package (e.g. a wheel URL that
        #     404s), the returned promise resolves normally and the failure
        #     is only delivered via the ``errorCallback`` option.
        #   * For an unknown package name, the returned promise rejects
        #     with a JavaScript ``Error``.
        #
        # Both paths are easy to miss in tests -- in the first case the
        # missing package surfaces later as a confusing
        # ``ModuleNotFoundError`` inside Pyodide. We normalize both into a
        # single ``RuntimeError`` raised at the call site so load failures
        # are always reported immediately and with the problematic package
        # reference in the message.
        result = self.run_js(f"""
            const __errors = [];
            try {{
                await pyodide.loadPackage({packages!r}, {{
                    errorCallback: (msg) => {{ __errors.push(msg); }},
                }});
            }} catch (e) {{
                __errors.push(e.message || String(e));
            }}
            return __errors;
            """)
        if result:
            raise RuntimeError(
                "pyodide.loadPackage({!r}) reported errors:\n  {}".format(
                    packages, "\n  ".join(result)
                )
            )


class _SeleniumBaseRunner(_BrowserBaseRunner):
    runner = "selenium"

    def goto(self, page):
        self.driver.get(page)

    def set_script_timeout(self, timeout):
        self.driver.set_script_timeout(timeout)
        self.script_timeout = timeout

    def quit(self):
        self.driver.quit()

    def refresh(self):
        self.driver.refresh()
        self.javascript_setup()

    def run_js_inner(self, code, check_code):
        wrapper = """
            let cb = arguments[arguments.length - 1];
            let run = async () => { %s }
            (async () => {
                try {
                    let result = await run();
                    %s
                    cb([0, result]);
                } catch (e) {
                    cb([1, e.toString(), e.stack, e.message]);
                }
            })()
        """
        retval = self.driver.execute_async_script(wrapper % (code, check_code))
        if retval[0] == 0:
            return retval[1]
        print("JavascriptException message: ", retval[3])
        raise JavascriptException(retval[1], retval[2])

    @property
    def urls(self):
        for handle in self.driver.window_handles:
            self.driver.switch_to.window(handle)
            yield self.driver.current_url


class _PlaywrightBaseRunner(_BrowserBaseRunner):
    runner = "playwright"

    def __init__(self, browsers, *args, **kwargs):
        self.browsers = browsers
        super().__init__(*args, **kwargs)

    def goto(self, page):
        self.driver.goto(page)

    def get_driver(self, jspi=False):
        if jspi:
            raise NotImplementedError("JSPI not supported with playwright")
        return self.browsers[self.browser].new_page()

    def set_script_timeout(self, timeout):
        # playwright uses milliseconds for timeout
        self.driver.set_default_timeout(timeout * 1000)

    def quit(self):
        self.driver.close()

    def refresh(self):
        self.driver.reload()
        self.javascript_setup()

    def run_js_inner(self, code, check_code):
        # playwright `evaluate` waits until primise to resolve,
        # so we don't need to use a callback like selenium.
        wrapper = """
            let run = async () => { %s }
            (async () => {
                try {
                    let result = await run();
                    %s
                    return [0, result];
                } catch (e) {
                    return [1, e.toString(), e.stack];
                }
            })()
        """
        retval = self.driver.evaluate(wrapper % (code, check_code))
        if retval[0] == 0:
            return retval[1]
        raise JavascriptException(retval[1], retval[2])


class SeleniumFirefoxRunner(_SeleniumBaseRunner):
    browser = "firefox"

    def get_driver(self, jspi=False):
        if jspi:
            raise NotImplementedError("JSPI not supported in Firefox")
        from selenium.webdriver import Firefox
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service

        options = Options()
        options.add_argument("--headless")
        for flag in self._config.get_flags("firefox"):
            options.add_argument(flag)

        return Firefox(service=Service(), options=options)


class SeleniumChromeRunner(_SeleniumBaseRunner):
    browser = "chrome"

    def get_driver(self, jspi=False):
        from selenium.webdriver import Chrome
        from selenium.webdriver.chrome.options import Options

        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        if jspi:
            options.add_argument("--enable-features=WebAssemblyExperimentalJSPI")
            options.add_argument("--enable-experimental-webassembly-features")
        for flag in self._config.get_flags("chrome"):
            options.add_argument(flag)
        return Chrome(options=options)

    def collect_garbage(self):
        self.driver.execute_cdp_cmd("HeapProfiler.collectGarbage", {})


# safaridriver exits with status 1 when the port it was told to use is already
# taken ("Unable to start the server: Address already in use"). Selenium picks
# that port with ``utils.free_port()``, which binds a socket, reads the port
# number and closes the socket again, so another process can claim the port
# before safaridriver gets to bind it. Retrying with a freshly constructed
# Service (and therefore a freshly picked port) works around that race.
SAFARI_START_RETRIES = 3
SAFARI_START_INTERVAL = 1.0
SAFARI_START_DEADLINE = 60.0

# Selenium reports every safaridriver startup problem as a bare
# WebDriverException, so the message is the only thing we can discriminate on.
# This is an allowlist on purpose: failing fast on an unrecognized error is far
# cheaper than burning the whole deadline retrying something that will never
# recover, such as a missing safaridriver, wrong file permissions, or "Allow
# Remote Automation" being disabled.
_SAFARI_RETRYABLE_ERRORS = (
    # Service.assert_process_still_running(): safaridriver died during startup.
    "unexpectedly exited",
    # Service.start(): the port never became connectable.
    "Can not connect to the Service",
)


def safari_startup_log_path() -> Path:
    """Return the path safaridriver's stdout/stderr is captured to.

    Selenium redirects the driver's output to ``os.devnull`` by default, which
    makes a failed startup impossible to diagnose from CI logs. Set
    ``PYTEST_PYODIDE_SAFARI_LOG`` to redirect it somewhere collectable as a CI
    artifact.
    """
    log_path = os.environ.get("PYTEST_PYODIDE_SAFARI_LOG")
    if log_path:
        return Path(log_path)
    return Path(tempfile.gettempdir()) / "pytest_pyodide_safaridriver.log"


def _read_log_tail(log_path: Path, max_lines: int = 20) -> str:
    try:
        contents = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(contents.splitlines()[-max_lines:]).strip()


def _is_retryable_safari_error(exc: BaseException) -> bool:
    from selenium.common.exceptions import NoSuchDriverException

    # NoSuchDriverException subclasses WebDriverException, but a driver that
    # cannot be found will not appear on a later attempt.
    if isinstance(exc, NoSuchDriverException):
        return False

    return any(marker in str(exc) for marker in _SAFARI_RETRYABLE_ERRORS)


def _discard_safari_service(service) -> None:
    """Reap a service whose ``start()`` failed.

    ``Service.start()`` does no cleanup of its own, so a safaridriver that
    launched but never became connectable is still running by the time the
    exception reaches us. We deliberately do not let ``Service.stop()`` deal
    with the process: it waits up to 60 seconds for a graceful exit, which we
    cannot afford between attempts. Clearing ``process`` first also keeps
    ``Service.__del__`` from re-entering that wait later on.
    """
    process = getattr(service, "process", None)
    service.process = None

    # Still call stop(): with ``process`` cleared it only closes the log file
    # handle, which we want released before the log is read back.
    try:
        service.stop()
    except Exception:
        pass

    if process is None or process.poll() is not None:
        return

    for terminate in (process.terminate, process.kill):
        try:
            terminate()
            process.wait(timeout=5)
            return
        except Exception:
            continue


def start_safari_service(
    *,
    retries: int = SAFARI_START_RETRIES,
    interval: float = SAFARI_START_INTERVAL,
    deadline: float = SAFARI_START_DEADLINE,
    log_path: Path | None = None,
):
    """Start a safaridriver service, retrying transient startup failures.

    Returns a started ``Service`` configured with ``reuse_service=True``, so the
    webdriver instances built on top of it neither restart nor stop it.
    """
    from selenium.webdriver.common.driver_finder import DriverFinder
    from selenium.webdriver.safari.options import Options
    from selenium.webdriver.safari.service import Service

    if log_path is None:
        log_path = safari_startup_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Resolve the driver binary once, up front. Doing this outside the loop means
    # a missing or invalid safaridriver raises NoSuchDriverException immediately
    # rather than being retried.
    driver_path = DriverFinder(Service(), Options()).get_driver_path()

    give_up_at = time.monotonic() + deadline
    last_exc: BaseException | None = None

    for attempt in range(1, retries + 1):
        # The port is chosen in ``Service.__init__``, so each attempt needs a new
        # object to get a new port.
        service = Service(reuse_service=True, log_output=str(log_path))
        service.path = driver_path

        try:
            service.start()
        except Exception as exc:
            _discard_safari_service(service)

            if not _is_retryable_safari_error(exc):
                raise

            last_exc = exc
            if attempt == retries or time.monotonic() >= give_up_at:
                break

            logger.warning(
                "safaridriver failed to start (attempt %d/%d), retrying in %.1fs: %s",
                attempt,
                retries,
                interval,
                exc,
            )
            time.sleep(interval)
        else:
            if attempt > 1:
                logger.warning(
                    "safaridriver started on attempt %d/%d", attempt, retries
                )
            return service

    log_tail = _read_log_tail(log_path)
    if log_tail:
        detail = f"\nsafaridriver output ({log_path}):\n{log_tail}"
    else:
        detail = f"\nNo safaridriver output was captured in {log_path}."

    raise RuntimeError(
        f"safaridriver failed to start after {retries} attempts.{detail}"
    ) from last_exc


@pytest.fixture(scope="session")
def use_global_safari_service():
    """Deprecated no-op, kept for backward compatibility."""
    yield None


class SeleniumSafariRunner(_SeleniumBaseRunner):
    browser = "safari"
    script_timeout = 30
    _service = None

    def get_driver(self, jspi=False):
        if jspi:
            raise NotImplementedError("JSPI not supported in Safari")
        from selenium.webdriver import Safari
        from selenium.webdriver.safari.options import Options

        # Start the service ourselves so a flaky launch can be retried and its
        # stderr captured. ``reuse_service=True`` keeps Safari from starting it a
        # second time, which makes stopping it our responsibility.
        self._service = start_safari_service()
        try:
            return Safari(options=Options(), service=self._service)
        except Exception:
            self._stop_service()
            raise

    def quit(self):
        try:
            super().quit()
        finally:
            self._stop_service()

    def _stop_service(self):
        service, self._service = self._service, None
        if service is None:
            return
        try:
            service.stop()
        except Exception:
            logger.warning("Failed to stop the safaridriver service", exc_info=True)


class _BrowserWorkerRunnerMixin(_BrowserBaseRunner):
    """Mixin that makes a Selenium-based runner execute JS inside a
    persistent Web Worker instead of on the main page.

    The main page still drives the browser (via Selenium), but each
    ``run_js_inner`` call is proxied to a long-lived worker. This lets us
    test Pyodide code paths that differ between the main thread and a
    worker context (e.g. ``importScripts``, ``FileReaderSync``, lack of
    DOM, etc.) across Chrome, Firefox, and Safari.
    """

    _worker = True

    WORKER_FILE = "module_webworker_runner.js"

    def prepare_driver(self):
        super().prepare_driver()
        # Boot a persistent worker on the page and install a small RPC
        # helper (``self.__workerCall``) that returns a promise resolved
        # with the worker's response for a given message id.
        worker_url = f"{self.base_url}/{self.WORKER_FILE}"
        bootstrap = f"""
            const worker = new Worker({worker_url!r}, {{ type: 'module' }});
            self.__worker = worker;
            self.__workerPending = new Map();
            self.__workerNextId = 1;
            worker.onmessage = (ev) => {{
                const {{ id }} = ev.data ?? {{}};
                const entry = self.__workerPending.get(id);
                if (!entry) {{ return; }}
                self.__workerPending.delete(id);
                entry(ev.data);
            }};
            worker.onerror = (ev) => {{
                // Reject every pending call with the worker error.
                const err = {{
                    ok: false,
                    error: ev.message ?? String(ev),
                    message: ev.message ?? "",
                    stack: ev.filename
                        ? `${{ev.filename}}:${{ev.lineno}}:${{ev.colno}}`
                        : "",
                }};
                for (const [id, entry] of self.__workerPending) {{
                    self.__workerPending.delete(id);
                    entry({{ id, ...err }});
                }}
            }};
            self.__workerCall = function (code) {{
                const id = self.__workerNextId++;
                return new Promise((resolve) => {{
                    self.__workerPending.set(id, resolve);
                    worker.postMessage({{ id, code }});
                }});
            }};
        """
        # Run the bootstrap on the main page itself
        super().run_js_inner(bootstrap, "")

    def run_js_inner(self, code, check_code):
        # Run ``code`` inside the worker; ``check_code`` must also run
        # inside the worker because it references ``globalThis.pyodide``
        # and ``pyodide._module`` which only exist on the worker's
        # ``self`` (they are set there by ``load_pyodide``). We fold both
        # into a single async IIFE so ``code``'s ``return`` value is
        # returned only after ``check_code`` has run.
        worker_body = f"""
            const __result = await (async () => {{ {code} }})();
            {check_code}
            return __result;
        """
        wrapper = f"""
            const __res = await self.__workerCall({worker_body!r});
            if (__res.ok) {{
                return __res.result;
            }}
            const __e = new Error(__res.message || __res.error);
            __e.stack = __res.stack;
            throw __e;
        """
        # check_code here is run on the page. We already handled it in worker_body
        return super().run_js_inner(wrapper, "")


class BrowserWorkerChromeRunner(_BrowserWorkerRunnerMixin, SeleniumChromeRunner):
    pass


class BrowserWorkerFirefoxRunner(_BrowserWorkerRunnerMixin, SeleniumFirefoxRunner):
    pass


class BrowserWorkerSafariRunner(_BrowserWorkerRunnerMixin, SeleniumSafariRunner):
    pass


class PlaywrightChromeRunner(_PlaywrightBaseRunner):
    browser = "chrome"

    def collect_garbage(self):
        client = self.driver.context.new_cdp_session(self.driver)
        client.send("HeapProfiler.collectGarbage")


class PlaywrightFirefoxRunner(_PlaywrightBaseRunner):
    browser = "firefox"


class NodeRunner(_BrowserBaseRunner):
    browser = "node"
    runner = "node"

    def init_node(self, jspi=False):
        curdir = Path(__file__).parent
        globals_str = json.dumps(self._config.get_node_extra_globals())
        env = os.environ.copy() | {
            "PYTEST_PYODIDE_NODE_TEST_DRIVER_EXTRA_GLOBALS": globals_str
        }
        self.p = pexpect.spawn("/bin/bash", timeout=60, env=env)
        self.p.setecho(False)
        self.p.delaybeforesend = None

        node_version = pexpect.spawn("node --version").read().decode("utf-8")
        node_major = int(node_version.split(".")[0][1:])  # vAA.BB.CC -> AA
        if node_major < 18:
            raise RuntimeError(
                f"Node version {node_version} is too old, please use node >= 18"
            )

        extra_args = self._config.get_flags("node")[:]
        # Node v14 require the --experimental-wasm-bigint which
        # produces errors on later versions
        if jspi:
            extra_args.append("--experimental-wasm-stack-switching")

        self.p.sendline(
            f"node --expose-gc {' '.join(extra_args)} {curdir}/node_test_driver.js {self.base_url} {self.dist_dir}",
        )

        try:
            self.p.expect_exact("READY!!")
        except (pexpect.exceptions.EOF, pexpect.exceptions.TIMEOUT):
            raise JavascriptException("", self.p.before.decode()) from None

    def get_driver(self, jspi=False):
        self._logs = []
        self.init_node(jspi)

        class NodeDriver:
            def __getattr__(self, x):
                raise NotImplementedError()

        return NodeDriver()

    def prepare_driver(self):
        pass

    def set_script_timeout(self, timeout):
        self.script_timeout = timeout

    def quit(self):
        self.p.sendeof()

    def refresh(self):
        self.quit()
        self.init_node()
        self.javascript_setup()

    def collect_garbage(self):
        self.run_js("gc()")

    @property
    def logs(self):
        return "\n".join(self._logs)

    def clean_logs(self):
        self._logs = []

    def run_js_inner(self, code, check_code):
        check_code = ""
        wrapped = f"""
            let result = await (async () => {{ {code} }})();
            {check_code}
            return result;
        """
        from uuid import uuid4

        cmd_id = str(uuid4())
        self.p.sendline(cmd_id)
        # split long lines into shorter buffers
        # because some ttys don't like long
        # single lines
        all_lines = wrapped.split("\n")
        for c, line in enumerate(all_lines):
            count = 0
            while count < len(line):
                to_read = min(128, len(line) - count)
                # each sent line ends with an extra $, to avoid problems with end-of-line
                # translation etc. it doesn't matter if there are $ in the string elsewhere
                # because the last $ is always the one we added
                self.p.sendline(line[count : count + to_read] + "$")
                # after we send a line, we wait for a response
                # before sending the next line
                # this means we don't overflow input buffers
                self.p.expect_exact("{LINE_OK}\r\n")
                count += to_read
            if c < len(all_lines) - 1:
                # to insert a line break into the received code string
                # we send a blank line with just the $ end-of-line character
                # and await response
                self.p.sendline("$")
                self.p.expect_exact("{LINE_OK}\r\n")
        self.p.sendline(cmd_id)
        self.p.expect_exact(f"{cmd_id}:UUID\r\n", timeout=self.script_timeout)
        self.p.expect_exact(f"{cmd_id}:UUID\r\n")
        if self.p.before:
            self._logs.append(self.p.before.decode()[:-2].replace("\r", ""))
        self.p.expect("[01]\r\n")
        success = int(self.p.match[0].decode()[0]) == 0
        self.p.expect_exact(f"\r\n{cmd_id}:UUID\r\n")
        if success:
            return json.loads(self.p.before.decode().replace("undefined", "null"))
        raise JavascriptException("", self.p.before.decode())
