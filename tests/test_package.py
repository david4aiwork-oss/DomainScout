def test_package_imports_and_has_version():
    import domainscout
    assert isinstance(domainscout.__version__, str)
    assert domainscout.__version__  # non-empty
