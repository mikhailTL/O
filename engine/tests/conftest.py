import pytest


@pytest.fixture
def tmp_data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d
