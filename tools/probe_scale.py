#!/usr/bin/env python3
"""Standalone BLE probe for Huawei Scale 3 / 3 Pro / 2 Pro (Hagrid WSP).

Runs *outside* Home Assistant, on any Linux/macOS host with a Bluetooth adapter
and ``bleak`` installed, using the very same protocol code as the integration.
Three jobs:

1. ``--scan``      — list nearby BLE devices and flag the Hagrid-family ones.
2. ``--gatt``      — connect and dump the full GATT table (services,
                     characteristics, properties). This is how you find out
                     whether a firmware exposes characteristics the integration
                     does not know about yet.
3. ``--session``   — run a complete connect → authenticate → profile sync →
                     measure cycle with the built-in CAK/C1/C2 keys, capture
                     every frame and write a text dump. Step on the scale when
                     prompted.

Examples
--------
    python tools/probe_scale.py --scan
    python tools/probe_scale.py --address AA:BB:CC:DD:EE:FF --gatt
    python tools/probe_scale.py --address AA:BB:CC:DD:EE:FF --session \
        --sex male --age 46 --height 178 --weight 91 --out scale_dump.txt

Run it with the repository root as the working directory (or set PYTHONPATH).
Prerequisite for ``--session``/``--gatt``: ``pip install bleak``.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import types
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "custom_components.huawei_scale_pro"
PACKAGE_PATH = REPO_ROOT / "custom_components" / "huawei_scale_pro"

# Import the package's pure-Python modules without executing its __init__
# (which needs Home Assistant).
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE] = package
sys.path.insert(0, str(REPO_ROOT))

from custom_components.huawei_scale_pro import protocol as p  # noqa: E402
from custom_components.huawei_scale_pro import session as s  # noqa: E402
from custom_components.huawei_scale_pro.const import (  # noqa: E402
    DEFAULT_C1,
    DEFAULT_C2,
    DEFAULT_CAK,
)

DEFAULT_TIMEOUT = 20.0


def _load_bleak():
    try:
        from bleak import BleakClient, BleakScanner  # noqa: PLC0415
    except ImportError:  # pragma: no cover - only on machines without bleak
        print(
            "bleak is required for --scan/--gatt/--session:  pip install bleak",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    return BleakClient, BleakScanner


def _profile_of(device) -> p.ProductProfile | None:
    """Identify a Hagrid device from a bleak advertisement."""
    manufacturer = dict(getattr(device, "manufacturer_data", {}) or {})
    service_data = {
        str(k): v for k, v in (getattr(device, "service_data", {}) or {}).items()
    }
    return p.identify_hagrid(device.name, manufacturer, service_data)


async def cmd_scan(timeout: float) -> int:
    _, BleakScanner = _load_bleak()
    print(f"Scanning for {timeout:.0f} s…")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    rows = []
    for device, adv in found.values():
        profile = _profile_of(adv)
        rows.append((bool(profile), device.address, device.name or "?", profile, adv))
    rows.sort(key=lambda row: (not row[0], row[1]))
    for is_hagrid, address, name, profile, adv in rows:
        tag = f" <-- {profile.display_name} ({profile.family})" if is_hagrid else ""
        print(f"{'*' if is_hagrid else ' '} {address}  {name}{tag}")
        if is_hagrid:
            print(f"    rssi={adv.rssi} caps={sorted(p.find_capability_bits(adv.name, adv.manufacturer_data, {str(k): v for k, v in adv.service_data.items()}))}")
            for uuid, data in (adv.manufacturer_data or {}).items():
                print(f"    manufacturer {uuid}: {data.hex().upper()}")
            for uuid, data in (adv.service_data or {}).items():
                print(f"    service {uuid}: {data.hex().upper()}")
    print(f"\n{sum(1 for row in rows if row[0])} Hagrid-family device(s) of {len(rows)}.")
    return 0


async def cmd_gatt(address: str, timeout: float, out: Path | None) -> int:
    BleakClient, _ = _load_bleak()
    async with BleakClient(address, timeout=timeout) as client:
        print(f"Connected to {address}")
        lines = [f"# GATT dump for {address} at {datetime.now().isoformat(timespec='seconds')}"]
        for service in client.services:
            lines.append(f"[service] {service.uuid}  {service.description}")
            for char in service.characteristics:
                props = ",".join(char.properties)
                lines.append(f"    [char] {char.uuid}  {props}  {char.description}")
                for desc in char.descriptors:
                    lines.append(f"        [desc] {desc.uuid}  {desc.description}")
        text = "\n".join(lines)
        print(text)
        if out:
            out.write_text(text + "\n", encoding="utf-8")
            print(f"\nWritten to {out}")
    return 0


async def cmd_session(
    address: str,
    user: s.ScaleUser,
    model: p.ProductProfile,
    *,
    timeout: float,
    read_history: bool,
    out: Path,
) -> int:
    BleakClient, _ = _load_bleak()
    capture = s.FrameCapture(limit=5000)
    secrets = s.Secrets.from_hex(DEFAULT_CAK, DEFAULT_C1, DEFAULT_C2)
    measurements: list[tuple[p.WeightMeasurement, bool]] = []

    def on_state(state: str) -> None:
        print(f"  state: {state}")

    def on_measurement(measurement: p.WeightMeasurement, live: bool) -> None:
        measurements.append((measurement, live))
        print(
            f"  {'live' if live else 'history'}: {measurement.weight_kg:.2f} kg, "
            f"fat {measurement.fat_percent:.1f} %, "
            f"z_low {measurement.impedance_ohm()} Ω, "
            f"z_high {measurement.impedance_high_ohm()} Ω, "
            f"hr {measurement.heart_rate}, len {measurement.raw_length}, "
            f"source {measurement.source}"
        )

    print(f"Model assumed: {model.display_name} ({model.family})")
    print("Step on the scale now (you have ~20 s)…")
    async with BleakClient(address, timeout=timeout) as client:
        session = s.ScaleSession(
            client,
            address,
            secrets,
            user,
            model=model,
            read_history=read_history,
            capture=capture,
            probe_unknown=True,
            on_state=on_state,
            on_measurement=on_measurement,
        )
        error: str | None = None
        try:
            await session.run()
        except Exception as err:  # noqa: BLE001 - the dump is the point, not the traceback
            error = f"{type(err).__name__}: {err}"
            print(f"  ! {error}")

    header = [
        "# huawei-scale-pro probe dump",
        f"# when: {datetime.now().isoformat(timespec='seconds')}",
        f"# address: {address}",
        f"# model: {model.display_name} ({model.family})",
        f"# user: id={user.user_id} male={user.male} age={user.age} "
        f"height={user.height_cm} weight={user.weight_kg}",
        f"# result: {'error: ' + error if error else 'ok'}",
        f"# measurements: {len(measurements)}",
        f"# frames: {len(capture)}",
    ]
    body = capture.as_text()
    out.write_text("\n".join(header) + "\n" + body + "\n", encoding="utf-8")
    print(f"\nDump written to {out} ({len(capture)} frames)")
    if error:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scan", action="store_true", help="list nearby BLE devices")
    parser.add_argument("--gatt", action="store_true", help="dump the GATT table")
    parser.add_argument("--session", action="store_true", help="run one full weigh-in")
    parser.add_argument("--address", help="scale Bluetooth address")
    parser.add_argument("--model", default="auto", choices=["auto", *p.MODEL_FAMILY_ALIASES])
    parser.add_argument("--sex", default="male", choices=["male", "female"])
    parser.add_argument("--age", type=int, default=40)
    parser.add_argument("--height", type=float, default=175)
    parser.add_argument("--weight", type=float, default=75)
    parser.add_argument("--name", default="probe")
    parser.add_argument("--history", action="store_true", help="also drain stored records")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--out", type=Path, help="write the dump/dump table to this file")
    args = parser.parse_args(argv)

    if not (args.scan or args.gatt or args.session):
        parser.print_help()
        return 0
    if (args.gatt or args.session) and not args.address:
        parser.error("--address is required with --gatt/--session")

    if args.model == "auto":
        model = p.profile_for_family(p.Family.SCALE_3_PRO)
    else:
        model = p.profile_for_family(p.MODEL_FAMILY_ALIASES[args.model])

    with contextlib.suppress(KeyboardInterrupt):
        if args.scan:
            return asyncio.run(cmd_scan(args.timeout))
        if args.gatt:
            out = args.out or Path(f"gatt_{args.address.replace(':', '')}.txt")
            return asyncio.run(cmd_gatt(args.address, args.timeout, out))
        user = s.ScaleUser(
            user_id=1234,
            male=args.sex == "male",
            age=args.age,
            height_cm=args.height,
            weight_kg=args.weight,
        )
        out = args.out or Path(f"scale_dump_{args.address.replace(':', '')}.txt")
        return asyncio.run(
            cmd_session(
                args.address, user, model,
                timeout=args.timeout, read_history=args.history, out=out,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
