"""What the tracker trusts for the house's own sites (Home Auth, the PulseChain node): the system's public CAs. They're
on *.home.selphfam.com names with a Let's Encrypt certificate."""
import ssl


def tls_context():
    return ssl.create_default_context()
