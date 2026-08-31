# keys/

Put your Kalshi RSA private key here, e.g. `kalshi_private_key.pem`.

Everything in this directory except this README is **gitignored** — never
commit a `.pem` file. Point `.env`'s `KALSHI_PRIVATE_KEY_PATH` at the file:

```
KALSHI_PRIVATE_KEY_PATH=./keys/kalshi_private_key.pem
```

Generate a key pair at kalshi.com → Account → API keys. Kalshi shows the
Key ID and lets you download the private key exactly once.
