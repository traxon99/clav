import clav


def test_package_importable() -> None:
    assert clav.__name__ == "clav"
