"""抓取前的 URL 安全校验（防 SSRF）。"""

import ipaddress
import socket
import urllib.parse

# ipaddress 标准属性未覆盖的运营商级 NAT 网段
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    if (
        ip.is_private        # 10/8, 172.16/12, 192.168/16, fc00::/7
        or ip.is_loopback    # 127/8, ::1
        or ip.is_link_local  # 169.254/16, fe80::/10
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    if ip.version == 4 and ip in _CGNAT:
        return True
    return False


def check_public_url(url: str) -> str | None:
    """校验 URL 是否指向可访问的公网地址。

    返回 None 表示通过；否则返回拒绝原因。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"不允许的协议：{parsed.scheme or '(无)'}"
    host = parsed.hostname
    if not host:
        return "无法解析 URL 主机名"

    # 主机名本身是 IP 字面量
    try:
        ipaddress.ip_address(host)
        if _is_blocked_ip(host):
            return f"禁止访问内网/保留地址：{host}"
        return None
    except ValueError:
        pass

    # 域名：解析成 IP 逐个校验
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"域名解析失败：{host}"

    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            return f"禁止访问内网/保留地址：{host} -> {ip}"
    return None
