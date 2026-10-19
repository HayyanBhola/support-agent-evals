import evalkit


def test_package_imports() -> None:
    assert evalkit.__name__ == "evalkit"
