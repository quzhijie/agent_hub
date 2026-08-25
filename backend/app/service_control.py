"""Small, launchd-bound controls for the local Agent Hub process."""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping


class LaunchdRestartController:
    """Exit once after the HTTP response; launchd's KeepAlive starts a new instance."""

    def __init__(
        self,
        service_name: str,
        *,
        environment: Mapping[str, str] | None = None,
        delay_seconds: float = 0.75,
        exit_process: Callable[[int], object] = os._exit,
    ) -> None:
        environment = os.environ if environment is None else environment
        self.available = environment.get("XPC_SERVICE_NAME") == service_name
        self._delay_seconds = delay_seconds
        self._exit_process = exit_process
        self._lock = threading.Lock()
        self._requested = False

    def request(self) -> bool:
        """Schedule one restart, returning False for a duplicate request."""
        if not self.available:
            raise RuntimeError("Agent Hub is not running as its launchd service")
        with self._lock:
            if self._requested:
                return False
            self._requested = True
        threading.Thread(
            target=self._exit_after_response,
            name="agent-hub-restart",
            daemon=True,
        ).start()
        return True

    def _exit_after_response(self) -> None:
        time.sleep(self._delay_seconds)
        self._exit_process(0)
