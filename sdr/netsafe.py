"""取得先 URL の安全確認（内部アドレス・ローカルファイル等の拒否）。"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443}
BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}


class UnsafeURLError(ValueError):
    pass


def normalize_url(url: str) -> str:
    """前後の空白除去・スキーム補完・フラグメント除去。"""
    url = (url or "").strip()
    if not url:
        raise UnsafeURLError("URLが空です")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def host_key(url: str) -> str:
    """重複判定用のホスト（www. を除く）。"""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def check_url_syntax(url: str) -> str:
    """構文だけで判定できる拒否（スキーム・認証情報・ポート・ホスト名・IP直書き）。"""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError(f"URLの形式が正しくありません: {exc}") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError("http/https 以外のURLは扱いません")
    if parts.username or parts.password:
        raise UnsafeURLError("認証情報を含むURLは扱いません")
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError("標準以外のポート番号のURLは扱いません")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURLError("ホスト名がありません")
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise UnsafeURLError("内部向けのホスト名は扱いません")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if "." not in host:
            raise UnsafeURLError("ドメイン名ではないホストは扱いません")
    else:
        if not is_public_ip(host.strip("[]")):
            raise UnsafeURLError("内部・予約済みのIPアドレスは扱いません")
    return host


def resolve_public(host: str) -> list[str]:
    """名前解決し、すべての解決先が公開アドレスであることを確認する。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"名前解決できません（{host}）") from exc
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise UnsafeURLError(f"名前解決できません（{host}）")
    for ip in ips:
        if not is_public_ip(ip.split("%", 1)[0]):
            raise UnsafeURLError(f"{host} は内部・予約済みのアドレス（{ip}）に解決されるため扱いません")
    return ips


def assert_safe_url(url: str, resolve: bool = True) -> None:
    host = check_url_syntax(url)
    if resolve:
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            resolve_public(host)
