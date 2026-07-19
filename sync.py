#!/usr/bin/env python3
"""Mirror the latest UniFi firmware for a fixed set of devices.

Two subcommands:
    sync      download the newest release firmware for every configured device
    resolve   look up the platform code for a device (by name, SKU or shortname)

Standard library only -- no pip install, runs on any Python 3.9+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

FIRMWARE_API = "https://fw-update.ubnt.com/api/firmware-latest"
DEVICE_DB = "https://static.ubnt.com/fingerprint/ui/public.json"
DEFAULT_CONFIG = "/etc/unifi-fw/config.json"

_device_db: list[dict[str, Any]] | None = None


def log(msg: str = "") -> None:
    print(msg, flush=True)


def fetch_json(url: str, timeout: int = 60) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


# --------------------------------------------------------------------------
# device database
# --------------------------------------------------------------------------

def device_db() -> list[dict[str, Any]]:
    """Ubiquiti's public device database, cached for the process lifetime."""
    global _device_db
    if _device_db is None:
        _device_db = fetch_json(DEVICE_DB)["devices"]
    return _device_db


def device_names(entry: dict[str, Any]) -> list[str]:
    """Every string a human might plausibly use to refer to this device."""
    product = entry.get("product") or {}
    names = [product.get("name"), product.get("abbrev"), entry.get("sku")]
    names += entry.get("shortnames") or []
    return [n for n in names if n]


def platform_of(entry: dict[str, Any]) -> str | None:
    """The firmware API's `platform` for a device database entry.

    This is `unifi.network.model` -- NOT the entry's shortname. The two often
    differ, and querying the firmware API by shortname silently returns an
    empty list rather than an error. Example: the USW Flex 2.5G 5 has
    shortname USM25G5 but platform USWED35.
    """
    return ((entry.get("unifi") or {}).get("network") or {}).get("model")


def find_devices(query: str) -> list[dict[str, Any]]:
    """Device database entries whose names match `query` (case-insensitive)."""
    q = query.strip().lower()
    exact, partial = [], []
    for entry in device_db():
        if not platform_of(entry):
            continue
        names = [n.lower() for n in device_names(entry)]
        if q in names:
            exact.append(entry)
        elif any(q in n for n in names):
            partial.append(entry)
    return exact or partial


def resolve_platform(query: str) -> str:
    """Map a device name/SKU/shortname to its firmware platform code."""
    matches = find_devices(query)
    if not matches:
        raise LookupError(f"no device matches {query!r} -- try: sync.py resolve {query!r}")
    platforms = {platform_of(m) for m in matches}
    if len(platforms) > 1:
        names = ", ".join(sorted(f"{(m.get('product') or {}).get('name')} -> {platform_of(m)}" for m in matches))
        raise LookupError(f"{query!r} is ambiguous ({names}) -- use the platform code directly")
    return platforms.pop()


# --------------------------------------------------------------------------
# firmware API
# --------------------------------------------------------------------------

def latest_firmware(platform: str, channel: str, product: str | None) -> dict[str, Any] | None:
    """Newest firmware for `platform`.

    A platform can carry several products at once: the UDM Pro, for instance,
    lists a current `unifi-dream` build alongside `unifi-firmware` and `udm`
    builds abandoned years ago. Without an explicit `product` we take whichever
    was released most recently, which picks the live line on its own.
    """
    url = f"{FIRMWARE_API}?filter=eq~~platform~~{platform}&filter=eq~~channel~~{channel}"
    items = fetch_json(url)["_embedded"]["firmware"]
    if product:
        items = [i for i in items if i["product"] == product]
    if not items:
        return None
    return max(items, key=lambda i: i.get("created", ""))


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path, proxy: str | None) -> None:
    cmd = ["curl", "-fsS", "--retry", "3", "--retry-delay", "5", "--max-time", "3600"]
    if proxy:
        # --socks5-hostname so DNS is resolved at the proxy too
        cmd += ["--socks5-hostname", proxy]
    cmd += ["-o", str(dest), url]
    subprocess.run(cmd, check=True)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config(path: str) -> dict[str, Any]:
    cfg = json.loads(Path(path).read_text())
    cfg.setdefault("dest", "/srv/firmware")
    cfg.setdefault("channel", "release")
    cfg.setdefault("keep", 1)
    cfg.setdefault("devices", [])
    # env wins, so the proxy URL can carry credentials without touching the file
    cfg["proxy"] = os.environ.get("UNIFI_FW_PROXY") or cfg.get("proxy")
    return cfg


def device_spec(item: Any) -> tuple[str, str | None, str]:
    """Normalise a config entry into (platform, product, label)."""
    if isinstance(item, str):
        return resolve_platform(item), None, item
    platform = item.get("platform") or resolve_platform(item["device"])
    return platform, item.get("product"), item.get("device") or platform


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

def cmd_resolve(args: argparse.Namespace) -> int:
    matches = find_devices(args.query)
    if not matches:
        log(f"nothing matches {args.query!r}")
        return 1
    for entry in sorted(matches, key=lambda e: (e.get("product") or {}).get("name") or ""):
        product = entry.get("product") or {}
        log(f"{product.get('name', '?')}")
        log(f"    platform : {platform_of(entry)}")
        log(f"    sku      : {entry.get('sku')}")
        log(f"    shortnames: {', '.join(entry.get('shortnames') or []) or '-'}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    dest = Path(cfg["dest"])
    if not args.check:
        dest.mkdir(parents=True, exist_ok=True)

    if not cfg["devices"]:
        log(f"no devices configured in {args.config}")
        return 1

    manifest: dict[str, Any] = {}
    downloaded = failed = stale = 0

    for item in cfg["devices"]:
        try:
            platform, product, label = device_spec(item)
        except LookupError as exc:
            log(f"!! {exc}")
            failed += 1
            continue

        try:
            firmware = latest_firmware(platform, cfg["channel"], product)
        except Exception as exc:
            log(f"[{platform}] firmware API failed: {exc}")
            failed += 1
            continue

        if not firmware:
            log(f"[{platform}] no {cfg['channel']} firmware found")
            failed += 1
            continue

        version = firmware["version"].lstrip("v").replace("+", "_")
        url = firmware["_links"]["data"]["href"]
        # not everything is a .bin -- the Network application ships as a .deb
        suffix = Path(urllib.parse.urlparse(url).path).suffix or ".bin"
        target = dest / f"{platform}-{version}{suffix}"
        want_sha = firmware.get("sha256_checksum")

        size_mb = firmware.get("file_size", 0) / 1e6

        if target.exists() and (not want_sha or sha256_of(target) == want_sha):
            log(f"[{platform}] {label}: up to date ({firmware['version']})")
        elif args.check:
            log(f"[{platform}] {label}: stale, would fetch {firmware['version']} ({size_mb:.1f} MB)")
            stale += 1
            continue
        else:
            log(f"[{platform}] {label}: downloading {firmware['version']} ({size_mb:.1f} MB)")
            partial = target.with_name(target.name + ".part")
            try:
                download(url, partial, cfg["proxy"])
                got = sha256_of(partial)
                if want_sha and got != want_sha:
                    partial.unlink(missing_ok=True)
                    log(f"[{platform}] !! checksum mismatch "
                        f"(want {want_sha[:12]}..., got {got[:12]}...)")
                    failed += 1
                    continue
                partial.replace(target)
                downloaded += 1
                log(f"[{platform}] saved {target.name}")
            except subprocess.CalledProcessError as exc:
                partial.unlink(missing_ok=True)
                hint = " -- HTTP 451? the CDN geo-blocks some regions, set a proxy" if not cfg["proxy"] else ""
                log(f"[{platform}] !! download failed ({exc}){hint}")
                failed += 1
                continue

        # --check reports and returns; it must never touch the mirror
        if args.check:
            continue

        # glob without a suffix: a platform can change extension between
        # releases, and the superseded file still has to go
        superseded = sorted(
            (p for p in dest.glob(f"{platform}-*") if p != target and p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in superseded[max(cfg["keep"] - 1, 0):]:
            log(f"[{platform}] removing superseded {old.name}")
            old.unlink()

        manifest[platform] = {
            "device": label,
            "product": firmware["product"],
            "version": firmware["version"],
            "file": target.name,
            "size": target.stat().st_size,
            "sha256": want_sha,
            "released": firmware.get("created", "")[:10],
        }

    log()
    if args.check:
        log(f"{stale} of {len(cfg['devices'])} devices stale, {failed} failed")
        return 1 if failed else 0

    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    log(f"{len(manifest)}/{len(cfg['devices'])} devices mirrored, "
        f"{downloaded} downloaded, {failed} failed")
    return 1 if failed else 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="download the newest firmware for configured devices")
    p_sync.add_argument("-c", "--config", default=os.environ.get("UNIFI_FW_CONFIG", DEFAULT_CONFIG))
    p_sync.add_argument("-n", "--check", action="store_true", help="report what is stale, download nothing")
    p_sync.set_defaults(func=cmd_sync)

    p_res = sub.add_parser("resolve", help="find the platform code for a device")
    p_res.add_argument("query", help="device name, SKU or shortname, e.g. 'Flex 2.5G'")
    p_res.set_defaults(func=cmd_resolve)

    args = parser.parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
