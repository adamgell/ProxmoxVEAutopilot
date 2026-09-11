"""Bounded readiness waiting for the disposable proof."""
import time
import errno
import urllib.error


def wait_for(check, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
        except urllib.error.HTTPError as error:
            error.close()
            if error.code != 503:
                raise
            result = None
        except urllib.error.URLError as error:
            if not isinstance(error.reason, OSError) or error.reason.errno != errno.ECONNREFUSED:
                raise
            result = None
        except ConnectionRefusedError:
            result = None
        if result:
            return result
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    raise AssertionError("bounded local proof deadline exceeded")
