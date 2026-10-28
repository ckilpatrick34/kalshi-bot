import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bot.client import KalshiClient, LiveTradingNotAllowed

PSS = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)


@pytest.fixture
def rsa_key(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", pem.decode())
    return key


def test_signature_covers_path_without_query(rsa_key):
    sig = KalshiClient(env="demo").sign("1703123456789", "get", "/trade-api/v2/portfolio/orders?limit=5")
    rsa_key.public_key().verify(
        base64.b64decode(sig), b"1703123456789GET/trade-api/v2/portfolio/orders", PSS, hashes.SHA256()
    )


def test_auth_headers_sign_full_api_path(rsa_key):
    headers = KalshiClient(env="demo")._auth_headers("GET", "/portfolio/balance")
    message = (headers["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/v2/portfolio/balance").encode()
    rsa_key.public_key().verify(base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), message, PSS, hashes.SHA256())


def test_prod_orders_blocked_without_explicit_flag(rsa_key, monkeypatch):
    monkeypatch.delenv("KALSHI_ALLOW_LIVE", raising=False)
    with pytest.raises(LiveTradingNotAllowed):
        KalshiClient(env="prod").create_order("T", "bid", 1, 0.5)
