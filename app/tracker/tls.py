"""What the tracker trusts for the house's own sites (Home Auth, the PulseChain node): the system's public CAs plus the house CA
when its file is there. Those sites are moving from .home names (house CA) to *.home.selphfam.com names (Let's
Encrypt) one at a time, so both must verify."""
import os
import ssl


def tls_context(ca=""):
    ctx = ssl.create_default_context()
    if ca and os.path.isfile(ca):
        ctx.load_verify_locations(cafile=ca)
    return ctx
