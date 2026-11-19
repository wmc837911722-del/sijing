"""Per-call evidence from the native process boundary."""
from contextlib import contextmanager
from contextvars import ContextVar


_observation = ContextVar('codex_dispatch', default=None)


class CodexDispatch:
    def __init__(self):
        self.launch_attempted = False
        self.unstarted_error = None
        self.control_error = None

    @contextmanager
    def observe(self):
        token = _observation.set(self)
        try:
            yield
        finally:
            _observation.reset(token)

    def check_control(self, callback):
        try:
            callback()
        except BaseException as error:
            self.control_error = error
            raise


@contextmanager
def native_attempt():
    dispatch = _observation.get() or CodexDispatch()
    try:
        yield dispatch
    except BaseException as error:
        if not dispatch.launch_attempted:
            dispatch.unstarted_error = error
        raise
