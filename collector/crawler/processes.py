"""Stop crawler subprocesses and their browser descendants."""
import logging
import os
import signal
import subprocess


def signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except PermissionError:
        # Some hosts restrict signals to a group while allowing its worker PID.
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            logging.getLogger(__name__).warning("Unable to signal collector process %s", pid)
    except ProcessLookupError:
        pass


def stop_group(proc):
    """The caller started this process with start_new_session=True."""
    signal_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The browser can survive the collector. Clean up before releasing its slot.
        signal_group(proc.pid, signal.SIGKILL)
    try:
        return proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return None
