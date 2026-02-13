def test_create_app_import():
    from ferias_app import create_app
    app = create_app()
    assert app is not None
