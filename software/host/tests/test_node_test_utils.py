import subprocess

from node_test_utils import run_node


def test_large_unicode_script_uses_stdin_below_platform_argument_limits(monkeypatch):
    original_run = subprocess.run
    observed = []

    def bounded_run(command, **options):
        assert sum(len(argument.encode("utf-8")) + 1 for argument in command) < 32768
        assert command == ["node", "-"]
        assert len(options["input"].encode("utf-8")) > 163840
        observed.append(command)
        return original_run(command, **options)

    monkeypatch.setattr(subprocess, "run", bounded_run)
    result = run_node("//" + "标定" * 30000 + "\nconsole.log('四点');", capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "四点"
    assert observed == [["node", "-"]]
