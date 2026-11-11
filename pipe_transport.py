"""A bounded, single-client Linux transport for Audacity's scripting FIFOs."""

import errno
import fcntl
import json
import os
import select
import stat
import time

_uncertain = False
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def quoted(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def checked_open(path, flags, *, fifo):
    """Refuse symlinks, unexpected file types/owners, and shared-writable IPC."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        expected = stat.S_ISFIFO(info.st_mode) if fifo else stat.S_ISREG(info.st_mode)
        if not expected or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise RuntimeError(
                "Unsafe scripting pipe or lock file. Check its owner, type, and permissions."
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


class Pipe:
    def __init__(self, *, allow_lifecycle=False, trace_limit=0):
        if not isinstance(trace_limit, int) or not 0 <= trace_limit <= 200:
            raise ValueError("The diagnostic trace limit must be between 0 and 200.")
        self.allow_lifecycle = allow_lifecycle
        self.trace_limit = trace_limit
        self.trace = []
        self._has_reply = False
        self.r = self.w = self.lock = None
        if _uncertain:
            raise RuntimeError(
                "The pipe is out of sync after a timeout. Restart Audacity and Repaint Align."
            )
        try:
            self.lock = checked_open(
                f"/tmp/repaint-align-pipe.{os.getuid()}.lock",  # nosec B108 # Owner/type/mode/link checks in checked_open.
                os.O_CREAT | os.O_RDWR,
                fifo=False,
            )
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.r = checked_open(
                f"/tmp/audacity_script_pipe.from.{os.getuid()}",  # nosec B108 # Required IPC path, validated by checked_open.
                os.O_RDONLY,
                fifo=True,
            )
            deadline = time.monotonic() + 3
            while True:
                try:
                    self.w = checked_open(
                        f"/tmp/audacity_script_pipe.to.{os.getuid()}",  # nosec B108 # Required IPC path, validated by checked_open.
                        os.O_WRONLY,
                        fifo=True,
                    )
                    break
                except OSError as exc:
                    if exc.errno != errno.ENXIO or time.monotonic() > deadline:
                        raise
                    time.sleep(0.05)
        except BaseException:
            self.close()
            raise

    def _poison(self):
        global _uncertain
        _uncertain = True
        self.close()

    def command(self, command, timeout=60, allow_failure=False):
        if command.split(":", 1)[0].strip() in ("Close", "Exit") and not self.allow_lifecycle:
            raise ValueError(
                "Close/Exit through the pipe are disabled: a crash was reproduced with Audacity 3.7.7. Close the window manually."
            )
        if self.w is None:
            raise RuntimeError("The Audacity pipe is closed.")
        message = (command + "\n").encode("utf-8")
        if len(message) > 1000 or "\n" in command or "\r" in command or "\x00" in command:
            raise ValueError("One command of at most 999 bytes required")
        os.write(self.w, message)
        chunks = []
        size = 0
        tail = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if select.select([self.r], [], [], 0.1)[0]:
                part = os.read(self.r, 65536)
                if part:
                    chunks.append(part)
                    size += len(part)
                    if size > MAX_RESPONSE_BYTES:
                        self._poison()
                        raise RuntimeError(
                            "Audacity returned too much metadata. Restart Audacity and Repaint Align."
                        )
                    tail = (tail + part)[-256:]
                    if b"BatchCommand finished:" in tail and tail.endswith(b"\n\n"):
                        break
                elif self._has_reply:
                    self._poison()
                    raise RuntimeError("Audacity disconnected. Restart Audacity and Repaint Align.")
                else:
                    time.sleep(0.01)
        response = b"".join(chunks).decode("utf-8")
        if self.trace_limit:
            self.trace.append(
                {"command": command, "response": response[:4096], "truncated": len(response) > 4096}
            )
            del self.trace[: -self.trace_limit]
        if "BatchCommand finished:" not in tail.decode("utf-8", errors="replace"):
            self._poison()
            raise TimeoutError(
                "Audacity did not reply in time. Do not repeat Apply. Check the clip position, then restart Audacity and Repaint Align."
            )
        self._has_reply = True
        if (
            "BatchCommand finished: OK" not in tail.decode("utf-8", errors="replace")
            and not allow_failure
        ):
            raise RuntimeError(
                "Audacity rejected a scripting command. Check Audacity for a dialog and capture again."
            )
        return response.rsplit("BatchCommand finished:", 1)[0].strip()

    def info(self, kind):
        return json.loads(self.command("GetInfo: Type=" + kind))

    def nyquist(self, code):
        return self.command("NyquistPrompt: Command=" + quoted(code) + ' Parameters=""')

    def close(self):
        # Wait for the old FIFO generation, keeping its response reader alive.
        # This closes our descriptors; it NEVER sends Audacity Close or Exit.
        writer = getattr(self, "w", None)
        if writer is not None:
            old = os.fstat(writer)
            os.close(writer)
            self.w = None
            deadline = time.monotonic() + 2
            path = f"/tmp/audacity_script_pipe.to.{os.getuid()}"  # nosec B108 # stat only; no file creation or write.
            while time.monotonic() < deadline:
                try:
                    current = os.stat(path)
                    if (current.st_ino, current.st_ctime_ns) != (old.st_ino, old.st_ctime_ns):
                        break
                except FileNotFoundError:
                    time.sleep(0.01)
                    continue
                time.sleep(0.01)
        for name in ["r", "lock"]:
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
