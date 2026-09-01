import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.kalshi_client import KalshiClient, best_prices


@pytest.fixture()
def client(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "test_key.pem"
    pem_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return KalshiClient(
        "https://demo-api.kalshi.co/trade-api/v2", "test-key-id", str(pem_path)
    )


def test_auth_headers_shape(client):
    headers = client.auth_headers("GET", "/portfolio/balance")
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()
    # signature must be valid base64
    base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])


def test_signature_verifies_with_public_key(client):
    headers = client.auth_headers("GET", "/portfolio/balance")
    ts = headers["KALSHI-ACCESS-TIMESTAMP"]
    message = f"{ts}GET/trade-api/v2/portfolio/balance".encode()
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    public_key = client._private_key.public_key()
    # raises InvalidSignature on failure
    public_key.verify(
        signature,
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def test_place_order_validates_inputs(client):
    import asyncio

    async def run():
        with pytest.raises(ValueError):
            await client.place_order("T", "maybe", "buy", 1, 50)
        with pytest.raises(ValueError):
            await client.place_order("T", "yes", "hold", 1, 50)
        with pytest.raises(ValueError):
            await client.place_order("T", "yes", "buy", 1, 150)

    asyncio.run(run())


def _capture_order_body(client):
    """Patch _request to capture the V2 order body instead of hitting the API."""
    captured = {}

    async def fake_request(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return {"order": {"order_id": "o1"}}

    client._request = fake_request
    return captured


@pytest.mark.parametrize(
    "side,action,price_cents,expect_side,expect_price",
    [
        ("yes", "buy", 55, "bid", "0.55"),
        ("yes", "sell", 55, "ask", "0.55"),
        ("no", "buy", 45, "ask", "0.55"),  # buy NO 45c == sell YES 55c
        ("no", "sell", 45, "bid", "0.55"),  # sell NO 45c == buy YES 55c
    ],
)
def test_place_order_v2_mapping(client, side, action, price_cents, expect_side, expect_price):
    import asyncio

    captured = _capture_order_body(client)
    asyncio.run(client.place_order("T", side, action, 3, price_cents))
    assert captured["method"] == "POST"
    assert captured["path"] == "/portfolio/events/orders"
    body = captured["json"]
    assert body["side"] == expect_side
    assert body["price"] == expect_price
    assert body["count"] == "3"
    assert body["time_in_force"] == "good_till_canceled"
    assert body["client_order_id"]


def test_best_prices_derivation():
    # Resting bids: best yes bid 40c, best no bid 55c
    book = {"yes": [[38, 100], [40, 20]], "no": [[55, 10], [50, 30]]}
    prices = best_prices(book)
    assert prices["yes_bid"] == 40
    assert prices["no_bid"] == 55
    assert prices["yes_ask"] == 45  # 100 - best no bid
    assert prices["no_ask"] == 60  # 100 - best yes bid


def test_best_prices_empty_book():
    prices = best_prices({"yes": [], "no": []})
    assert prices == {"yes_bid": None, "no_bid": None, "yes_ask": None, "no_ask": None}
