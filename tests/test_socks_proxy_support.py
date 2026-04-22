import requests


def test_requests_supports_socks5_proxy_manager():
    session = requests.Session()

    proxy_manager = session.get_adapter("https://").proxy_manager_for(
        "socks5://127.0.0.1:1080"
    )

    assert proxy_manager is not None
