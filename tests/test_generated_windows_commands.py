from pathlib import Path

from app.data import runtime


def test_generated_windows_commands_use_the_active_d_checkout() -> None:
    repository_root = Path(runtime.__file__).resolve().parents[2]
    assert repository_root.drive.upper() == "D:"

    commands = {
        name: value
        for name, value in vars(runtime).items()
        if name.endswith("_WINDOWS_CMD") and isinstance(value, str)
    }
    assert commands

    expected_prefix = f'cd /d "{repository_root}" && '
    for name, command in commands.items():
        assert command.startswith(expected_prefix), name
        assert "C:\\Users\\simon\\Documents\\Codex" not in command, name
