import http.client
import ipaddress
import os
import socket
import ssl
import time
from urllib.parse import urlsplit

from ..config import MAX_BYTES


class IngestError(ValueError):
    pass


RESTRICTED = ("youtube.com", "youtu.be", "googlevideo.com", "tiktok.com", "instagram.com", "facebook.com")


def url_mode():
    if os.getenv("MEDIA_URL_HOSTS", "").strip(", "):
        return "allowlist"
    if os.getenv("ALLOW_PUBLIC_MEDIA_URLS") == "1":
        return "public"
    return "disabled"


def public_address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address):
        address = address.ipv4_mapped or address
    return address.is_global and not address.is_multicast and not address.is_reserved


def resolve(url):
    try:
        parts = urlsplit(url)
        port = parts.port or 443
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        raise IngestError("Invalid media URL") from None
    if any(host == domain or host.endswith("." + domain) for domain in RESTRICTED):
        raise IngestError("This platform link requires an official export. Upload a video file you are entitled to process")
    if (parts.scheme != "https" or port != 443 or parts.username is not None or parts.password is not None
            or not host or parts.fragment):
        raise IngestError("Use a direct HTTPS video file URL without embedded credentials or fragments")
    mode = url_mode()
    hosts = {host.strip().lower() for host in os.getenv("MEDIA_URL_HOSTS", "").split(",") if host.strip()}
    if mode == "disabled":
        raise IngestError("Direct URL ingestion is disabled on this server. Upload the video file instead")
    if mode == "allowlist" and host not in hosts:
        raise IngestError("This video host is outside the server allowlist")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)}
    except OSError:
        raise IngestError("The media host could not be resolved") from None
    if not addresses or any(not public_address(ip)
                            for ip in addresses):
        raise IngestError("The media host must resolve exclusively to public addresses")
    return parts, sorted(addresses)[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, 443, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        connection = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except Exception:
            connection.close()
            raise


def fetch(url, output):
    parts, address = resolve(url)
    connection = PinnedHTTPS(parts.hostname, address)
    deadline = time.monotonic() + 300
    try:
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection.request("GET", target, headers={"Accept": "video/*, application/octet-stream"})
        response = connection.getresponse()
        if response.status != 200:
            raise IngestError("The media host must return a file directly with HTTP 200. Redirects are disabled")
        content_type = (response.getheader("Content-Type") or "").lower()
        if content_type.startswith(("text/", "application/json", "application/xml")):
            raise IngestError("This URL returned a webpage or document. Use the direct video file URL")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
            raise IngestError("The source exceeds the upload limit")
        size = 0
        with output.open("wb") as handle:
            while chunk := response.read1(128 * 1024):
                size += len(chunk)
                if size > MAX_BYTES or time.monotonic() > deadline:
                    raise IngestError("The source exceeded the size or download time limit")
                handle.write(chunk)
        return size
    except (OSError, http.client.HTTPException):
        raise IngestError("Could not download the approved source") from None
    finally:
        connection.close()
