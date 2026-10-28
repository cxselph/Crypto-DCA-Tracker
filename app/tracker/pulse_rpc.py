"""Relays the page's on-chain price lookups to our own PulseChain node (PULSECHAIN_RPC_URL, e.g. https://rpc.home).

The node only answers its own site (no CORS for other origins), so the page can't call it directly; it asks
this server instead, and falls back to the public RPCs when this fails. Only the four read-only calls the
price fallback makes are passed on (a pair's token0/token1/getReserves and a token's decimals, at 'latest'),
so this is not a general-purpose proxy to the node. TLS is checked against the .home CA (the same certificate
Home Auth sign-in uses). Standard library only.
"""

import json
import re
import ssl
import urllib.error
import urllib.request

# token0(), token1(), getReserves(), decimals()
ALLOWED_SELECTORS = {"0x0dfe1681", "0xd21220a7", "0x0902f1ac", "0x313ce567"}
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TIMEOUT_SECONDS = 3  # under the page's own 4 s, so it can still move on to a public RPC
MAX_REQUEST = 4096


class RelayError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def check_call(raw):
    """The (to, data) of an allowed eth_call request body, or RelayError(400)."""
    if not raw or len(raw) > MAX_REQUEST:
        raise RelayError(400, "Empty or oversized request")
    try:
        req = json.loads(raw)
    except ValueError:
        raise RelayError(400, "Invalid JSON")
    if not isinstance(req, dict) or req.get("method") != "eth_call":
        raise RelayError(400, "Only eth_call is relayed")
    params = req.get("params")
    if not (isinstance(params, list) and len(params) == 2 and params[1] == "latest" and isinstance(params[0], dict)):
        raise RelayError(400, "Expected [{to, data}, 'latest']")
    call = params[0]
    to, data = call.get("to"), call.get("data")
    if set(call) != {"to", "data"} or not isinstance(to, str) or not ADDRESS.match(to):
        raise RelayError(400, "Expected a contract address in 'to'")
    if not isinstance(data, str) or data.lower() not in ALLOWED_SELECTORS:
        raise RelayError(400, "That call isn't relayed")
    return to, data.lower()


def relay(raw, url, ca=""):
    """Sends an allowed eth_call to the node and returns its JSON-RPC answer (a dict with result or error)."""
    if not url:
        raise RelayError(503, "No PulseChain node set (PULSECHAIN_RPC_URL)")
    to, data = check_call(raw)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]})
    req = urllib.request.Request(url, data=body.encode(), headers={"Content-Type": "application/json"})
    ctx = ssl.create_default_context(cafile=ca or None)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=ctx) as r:
            answer = json.loads(r.read(64 * 1024))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RelayError(502, f"The node didn't answer: {e}")
    if not isinstance(answer, dict) or ("result" not in answer and "error" not in answer):
        raise RelayError(502, "The node's answer wasn't JSON-RPC")
    return {"jsonrpc": "2.0", "id": 1, **{k: answer[k] for k in ("result", "error") if k in answer}}
