import threading

import pytest

from app.service_control import LaunchdRestartController


def test_restart_controller_is_only_available_inside_its_launchd_service():
    controller = LaunchdRestartController(
        "com.agent-hub", environment={"XPC_SERVICE_NAME": "another-service"},
    )

    assert controller.available is False
    with pytest.raises(RuntimeError, match="launchd"):
        controller.request()


def test_restart_controller_schedules_exactly_one_exit():
    exited = threading.Event()
    codes = []
    controller = LaunchdRestartController(
        "com.agent-hub",
        environment={"XPC_SERVICE_NAME": "com.agent-hub"},
        delay_seconds=0,
        exit_process=lambda code: (codes.append(code), exited.set()),
    )

    assert controller.request() is True
    assert controller.request() is False
    assert exited.wait(1)
    assert codes == [0]
