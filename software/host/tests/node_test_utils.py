import subprocess


def run_node(script, **options):
    return subprocess.run(["node", "-"], input=script, encoding="utf-8", **options)
