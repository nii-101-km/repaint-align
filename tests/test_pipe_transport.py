import pytest

from pipe_transport import Pipe


@pytest.mark.parametrize("command", ["Close:", "Exit:"])
def test_crashing_lifecycle_commands_are_rejected_before_writing(command, monkeypatch):
    pipe = Pipe.__new__(Pipe)
    pipe.w = 1
    pipe.allow_lifecycle = False

    def unexpected_write(*args):
        pytest.fail("unsafe lifecycle command reached FIFO")

    monkeypatch.setattr("pipe_transport.os.write", unexpected_write)
    with pytest.raises(ValueError, match="disabled"):
        pipe.command(command)
