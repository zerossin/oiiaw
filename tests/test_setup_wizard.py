"""
Regression test for a real bug found migrating a live vault: build_config()
only wrote the "paths" section, so wizard-generated config.yaml had none of
config.example.yaml's default ignore rules. First sync then flagged
.obsidian/workspace.json (legitimately different per device) and leftover
files in Obsidian's own .Trash as real conflicts.
"""

import os

import yaml

from oiiaw.setup_wizard import build_config

EXAMPLE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.example.yaml")


def test_build_config_ignore_defaults_match_example_yaml():
    with open(EXAMPLE_PATH, "r", encoding="utf-8") as f:
        example = yaml.safe_load(f)

    generated = build_config(r"C:\local", r"C:\cloud")["ignore"]

    assert generated["dirs"] == example["ignore"]["dirs"]
    assert generated["files"] == example["ignore"]["files"]
    assert generated["patterns"] == example["ignore"]["patterns"]


def test_build_config_uses_a_stable_baseline_per_vault_pair():
    first = build_config(r"C:\local", r"C:\cloud")["paths"]["sync_baseline"]
    same = build_config(r"C:\local", r"C:\cloud")["paths"]["sync_baseline"]
    changed = build_config(r"C:\other", r"C:\cloud")["paths"]["sync_baseline"]

    assert first == same
    assert first != changed
