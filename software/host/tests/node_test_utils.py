import subprocess


def run_node(script, **options):
    options.setdefault("timeout", 30)
    return subprocess.run(["node", "-"], input=script, encoding="utf-8", **options)
