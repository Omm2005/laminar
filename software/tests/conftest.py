import pytest

from laminar import telemetry
from laminar.hardware import make_system


@pytest.fixture(autouse=True)
def tmp_log(tmp_path):
    telemetry.configure(tmp_path / "telemetry.jsonl", run_id="test")
    yield tmp_path / "telemetry.jsonl"
    telemetry.configure(run_id=None)


@pytest.fixture
def system():
    return make_system(fake=True, time_scale=0)
