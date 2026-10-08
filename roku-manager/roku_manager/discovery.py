"""Find Roku devices on the local network.

Two methods:
  * SSDP: the standard multicast search Roku devices answer ("roku:ecp").
  * Subnet scan: a fallback that probes port 8060 on every address in a
    subnet, for networks where multicast is blocked.
"""

import ipaddress
import socket
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from .ecp import ECP_PORT, EcpClient, EcpError

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_REQUEST = (
    "M-SEARCH * HTTP/1.1\r\n"
    "Host: 239.255.255.250:1900\r\n"
    'Man: "ssdp:discover"\r\n'
    "ST: roku:ecp\r\n"
    "MX: 2\r\n"
    "\r\n"
).encode()

MAX_SCAN_HOSTS = 1024


def parse_ssdp_location(packet):
    """Return (host, port) from an SSDP response, or None."""
    text = packet.decode("utf-8", "replace")
    for line in text.split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "location":
            url = urllib.parse.urlparse(value.strip())
            if url.hostname:
                return url.hostname, url.port or ECP_PORT
    return None


def ssdp_discover(timeout=3.0):
    found = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        for _ in range(2):
            sock.sendto(SSDP_REQUEST, SSDP_ADDR)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                packet, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            location = parse_ssdp_location(packet)
            if location:
                found.add(location)
    finally:
        sock.close()
    return sorted(found)


def local_ipv4():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent
        return s.getsockname()[0]
    finally:
        s.close()


def default_subnet():
    return str(ipaddress.ip_network(f"{local_ipv4()}/24", strict=False))


def scan_subnet(cidr=None, port=ECP_PORT, timeout=0.4, workers=64):
    network = ipaddress.ip_network(cidr or default_subnet(), strict=False)
    if network.num_addresses > MAX_SCAN_HOSTS:
        raise ValueError(f"Subnet {network} is too large to scan (max {MAX_SCAN_HOSTS} addresses)")

    def probe(ip):
        try:
            with socket.create_connection((str(ip), port), timeout=timeout):
                return (str(ip), port)
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(probe, network.hosts())
    return [r for r in results if r]


def identify(host, port=ECP_PORT):
    """Return device-info for host if it is a Roku, else None."""
    try:
        info = EcpClient(host, port).device_info()
    except EcpError:
        return None
    return info if info.get("serial-number") or info.get("device-id") else None


def discover(method="ssdp", subnet=None):
    """Return a list of (host, device_info) for Rokus found on the network."""
    candidates = ssdp_discover() if method == "ssdp" else scan_subnet(subnet)
    with ThreadPoolExecutor(max_workers=16) as pool:
        infos = list(pool.map(lambda c: identify(*c), candidates))
    return [(host, info) for (host, _), info in zip(candidates, infos) if info]
