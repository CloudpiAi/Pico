"""Run a child process for a scheduled task (an `ask` answer, a cost-data read) without leaving anything behind.

`run` is a small stand-in for subprocess.run(capture_output=True, text=True): the child gets its own process group,
so on a timeout (or any error) the child and everything it started (MCP servers, shells) are killed together, and
after a normal exit anything it left running is killed too. Output goes to temporary files and only the last
`max_output` characters come back, so a runaway child can't fill the agent's memory.
"""
import os
import signal
import subprocess
import tempfile

OUTPUT_MAX = 4_000_000  # characters of stdout kept (the end: an `ask` answer is its last lines)
ERROR_MAX = 64_000


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _tail(fh, limit):
    fh.flush()
    size = fh.seek(0, os.SEEK_END)
    fh.seek(max(0, size - limit * 4))  # up to 4 bytes per character
    return fh.read().decode("utf-8", errors="replace")[-limit:]


def run(cmd, *, timeout, env, input=None, max_output=OUTPUT_MAX, **_ignored):
    """subprocess.CompletedProcess(cmd, returncode, stdout, stderr) with text output; raises
    subprocess.TimeoutExpired after killing the child's whole process group."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                stdout=out, stderr=err, env=env, start_new_session=True)
        try:
            if input is not None:
                try:
                    proc.stdin.write(input.encode("utf-8"))
                except BrokenPipeError:  # it exited without reading: its exit code says why
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except BrokenPipeError:
                        pass
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
            raise subprocess.TimeoutExpired(cmd, timeout) from None
        except BaseException:
            _kill_group(proc)
            proc.wait()
            raise
        _kill_group(proc)  # anything it started and left running
        return subprocess.CompletedProcess(cmd, rc, _tail(out, max_output), _tail(err, ERROR_MAX))
