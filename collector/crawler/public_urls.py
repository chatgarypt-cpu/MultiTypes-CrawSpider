"""URL checks for requests originating from the dashboard."""
import ipaddress
import os
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests


def normalize_url(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请输入域名或网址')
    value = value.strip()
    p = urlsplit(value if '://' in value else 'https://' + value)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('仅支持不含账号密码的 HTTP / HTTPS 网址')
    host = p.hostname.encode('idna').decode().lower().rstrip('.')
    if not re.fullmatch(r'[a-z0-9.-]+', host) or '..' in host:
        raise ValueError('域名格式不正确')
    port = p.port
    if port and port not in (80, 443) and os.getenv('CRAWL_ALLOW_PRIVATE') != '1':
        raise ValueError('公开站点仅支持 80 / 443 端口')
    if os.getenv('CRAWL_ALLOW_PRIVATE') != '1':
        try:
            addresses = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError('域名无法解析，请检查拼写或网络') from exc
        # macOS TUN resolvers may map public names into the RFC 2544 fake-IP range.
        # Literal private IPs remain forbidden. This is a trusted-operator service,
        # not a hardened arbitrary-URL public proxy.
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        fake = ipaddress.ip_network('198.18.0.0/15')
        if (literal and not literal.is_global) or any(
                not ipaddress.ip_address(a[4][0]).is_global and
                not (literal is None and ipaddress.ip_address(a[4][0]) in fake) for a in addresses):
            raise ValueError('不能提交本机或内网地址')
    authority = host + (f':{port}' if port else '')
    return urlunsplit((p.scheme, authority, p.path or '/', p.query, ''))


def public_get(get, url, *, same_host=None, allow_target=None, **kwargs):
    """Check each redirect before sending it, including the initial target."""
    for _ in range(requests.sessions.DEFAULT_REDIRECT_LIMIT + 1):
        target = normalize_url(url)
        if same_host and urlsplit(target).netloc != same_host:
            raise ValueError("页面跳转到站点范围之外")
        if allow_target and not allow_target(target):
            raise PermissionError("robots.txt 禁止访问此路径")
        response = get(target, allow_redirects=False, **kwargs)
        if not response.is_redirect:
            return response
        url = urljoin(target, response.headers["Location"])
        response.close()
    raise ValueError("页面跳转次数过多")
