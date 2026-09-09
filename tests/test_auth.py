import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from kalshitrader.kalshi.auth import RequestSigner, load_private_key, signing_path


def test_signing_path_strips_query():
    assert signing_path("https://api.elections.kalshi.com/trade-api/v2/markets?limit=5&status=open") == "/trade-api/v2/markets"


def test_headers_verify_with_public_key(rsa_pem):
    key = load_private_key(pem=rsa_pem)
    signer = RequestSigner("key-abc", key)
    url = "https://demo-api.kalshi.co/trade-api/v2/portfolio/balance?x=1"
    headers = signer.headers("get", url, timestamp_ms=1700000000000)
    assert headers["KALSHI-ACCESS-KEY"] == "key-abc"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    message = b"1700000000000GET/trade-api/v2/portfolio/balance"
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_load_private_key_from_path(tmp_path, rsa_pem):
    p = tmp_path / "k.pem"
    p.write_text(rsa_pem)
    key = load_private_key(path=p)
    assert key.key_size == 2048
    assert isinstance(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), bytes)
