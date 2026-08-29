import os

from oiiaw.config import Config, paths_overlap


def test_missing_cloud_vault_is_retryable(tmp_path):
    config = Config({
        "paths": {
            "local_vault": str(tmp_path / "local"),
            "cloud_vault": str(tmp_path / "missing-cloud"),
        }
    })

    problems = config.validate()

    assert any(not problem.blocking and "cloud_vault" in problem.message for problem in problems)


def test_nested_vault_paths_are_rejected(tmp_path):
    local = tmp_path / "vault"
    cloud = local / "icloud"
    os.makedirs(cloud)

    assert paths_overlap(str(local), str(cloud)) is True
    config = Config({"paths": {"local_vault": str(local), "cloud_vault": str(cloud)}})
    assert any(problem.blocking and "contain each other" in problem.message for problem in config.validate())
