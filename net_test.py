import socket
import ssl
import urllib.request

print("Testing OpenAI connectivity...")

# DNS test
try:
    ip = socket.gethostbyname("api.openai.com")
    print("DNS OK:", ip)
except Exception as e:
    print("DNS FAILED:", repr(e))

# HTTPS test
try:
    ctx = ssl.create_default_context()
    with urllib.request.urlopen("https://api.openai.com/v1/models", context=ctx, timeout=15) as r:
        print("HTTPS OK, status:", r.status)
except Exception as e:
    print("HTTPS FAILED:", repr(e))
