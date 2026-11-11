"""Protocol tests, anchored on the real Scale 3 capture from openScale's tests.

The framing, CRC, key derivation and measurement decoding are bit-identical to
the upstream Huawei Scale 3 port this project is based on, so the real capture
still has to reproduce exactly. The Scale 3 Pro specific parts (model table,
gender encoding, dual-frequency and standard-characteristic payloads) are tested
on top of that.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.huawei_scale_pro import protocol as p

# ---- Real capture (HUAWEI Scale 3, 38:1E:C7:66:08:D3), from openScale's test suite
CAK_UNUSED = bytes(16)
C1 = bytes.fromhex("CA4946D061C9FE534F6044F930EBB69B")
C2 = bytes.fromhex("FBCE6E2B4BAF80ED969BA26B4A4B9325")
ADDRESS = "38:1E:C7:66:08:D3"
ROOT_KEY = bytes.fromhex("d680f0f3b50576a74c8815653e8b7243")
WORK_KEY = bytes.fromhex("36697370363671706d64386733696b62")
WORK_KEY_FRAMES = [
    "dc12203b033efce70954e24a3a06ac75603b418e",
    "dc12218a68cb34e5250a6c185813b62e6958c13f",
    "dc0522a49ddc06",
]
MEASUREMENT_FRAMES = [
    "cd1220ddae5ad500d352ad002d9ab60055a313f1",
    "cd12216d4c5b1e4fa529d8fc4225d8db8b3726b3",
    "cd0f222285c5b012308c6ee4a7829efa52",
]
MEASUREMENT_PLAIN = bytes.fromhex("b1212d01ea07080917271da0ea13000000000000000000006100")


def _deframe(frames: list[str]) -> bytes:
    return b"".join(bytes.fromhex(f)[3:-2] for f in frames)


def test_crc_table_matches_openscale_constants():
    assert p._CRC16_TABLE[1] == 4129
    assert p._CRC16_TABLE[16] == 4657
    assert p._CRC16_TABLE[255] == 7920


def test_root_key_matches_capture():
    c3 = p.c3_from_bluetooth_address(ADDRESS)
    assert c3 == b"381EC76608D30000"
    assert p.derive_root_key(C1, C2, c3) == ROOT_KEY


def test_work_key_decrypts_from_capture():
    assert p.decrypt_payload(_deframe(WORK_KEY_FRAMES), ROOT_KEY) == WORK_KEY


def test_write_frames_are_bit_identical_to_the_capture():
    """Our encoder (incl. XMODEM CRC) must reproduce what the phone sent."""
    iv = _deframe(WORK_KEY_FRAMES)[:16]
    frames = p.build_encrypted_write_frames(WORK_KEY, ROOT_KEY, iv)
    assert [f.hex() for f in frames] == WORK_KEY_FRAMES


def test_notification_frames_have_valid_modbus_crc():
    for hexframe in MEASUREMENT_FRAMES:
        assert p.has_valid_notification_crc(bytes.fromhex(hexframe))


def test_accumulator_reassembles_and_decrypts_capture():
    acc = p.FrameAccumulator()
    out = None
    for hexframe in MEASUREMENT_FRAMES:
        out = acc.ingest(bytes.fromhex(hexframe))
    assert out is not None
    assert acc.last_completed_encrypted is True
    assert p.decrypt_payload(out, WORK_KEY) == MEASUREMENT_PLAIN


def test_capture_decodes_to_expected_measurement():
    m = p.parse_realtime_measurement(MEASUREMENT_PLAIN)
    assert m is not None
    assert m.weight_kg == pytest.approx(86.25)
    assert m.fat_percent == pytest.approx(30.1)
    assert m.heart_rate == 97
    assert m.timestamp == datetime(2026, 8, 9, 23, 39, 29)
    assert m.low_impedance[0] == 0x13EA
    assert m.impedance_ohm() == pytest.approx(509.8)
    assert m.raw_payload == MEASUREMENT_PLAIN


def test_corrupt_notification_crc_is_dropped():
    frame = bytearray(bytes.fromhex(MEASUREMENT_FRAMES[0]))
    frame[5] ^= 0xFF
    assert p.FrameAccumulator().ingest(bytes(frame)) is None


def _to_notification(write_frame: bytes) -> bytes:
    frame = bytearray(write_frame)
    frame[0] = p.FRAME_NOTIFY_PLAIN
    crc = p.crc16_modbus(bytes(frame[:-2]))
    frame[-2], frame[-1] = crc & 0xFF, crc >> 8
    return bytes(frame)


def test_multi_frame_roundtrip():
    payload = bytes(range(40))
    frames = p.build_write_frames(payload)
    assert len(frames) == 3
    acc = p.FrameAccumulator()
    assert [acc.ingest(_to_notification(f)) for f in frames] == [None, None, payload]


def test_empty_payload_is_single_frame():
    (frame,) = p.build_write_frames(b"")
    assert len(frame) == 5 and frame[:3] == bytes([0xDB, 0x03, 0x00])


def test_accumulator_drops_orphan_and_restarts_on_index_zero():
    a = p.build_write_frames(bytes(range(26)))
    b = p.build_write_frames(bytes(range(100, 126)))
    acc = p.FrameAccumulator()
    assert acc.ingest(_to_notification(a[1])) is None  # orphan
    assert acc.ingest(_to_notification(a[0])) is None
    assert acc.ingest(_to_notification(b[0])) is None  # restart
    assert acc.ingest(_to_notification(b[1])) == bytes(range(100, 126))


def test_oversized_payload_rejected():
    with pytest.raises(ValueError):
        p.build_write_frames(bytes(241))


def test_auth_handshake_is_symmetric_and_checks_cak():
    rand_a, rand_b = bytes(range(16)), bytes(range(16, 32))
    cak = bytes(range(32, 48))
    token = p.build_auth_token_payload(rand_a, rand_b, cak)
    assert token[:16] == rand_b and len(token) == 48
    resp = p.expected_auth_response(rand_a, rand_b, cak)
    assert len(resp) == 32
    assert p.is_valid_auth_response(resp, rand_a, rand_b, cak)
    assert p.is_valid_auth_response(resp + b"\x00", rand_a, rand_b, cak)
    assert not p.is_valid_auth_response(resp[:-1], rand_a, rand_b, cak)
    assert not p.is_valid_auth_response(resp, rand_a, rand_b, bytes(16))


def test_user_info_layout():
    payload = p.build_user_info_payload(
        huid="openscale:001122334455",
        uid="u:00000007",
        gender=1,
        age_years=26,
        height_cm=171,
        weight_kg=83.2,
    )
    assert len(payload) == 69
    assert payload[62] == 1 and payload[63] == 26
    assert payload[64:66] == bytes([171, 0])
    assert payload[66:68] == bytes([0x80, 0x20])  # 8320 centi-kg
    assert payload.startswith(b"openscale:001122334455\x00")


def test_current_time_payload():
    assert p.current_time_payload(datetime(2026, 8, 10, 7, 5, 9)) == bytes(
        [0xEA, 0x07, 8, 10, 7, 5, 9, 1]
    )


def _realtime_payload() -> bytes:
    return bytes(
        [0x34, 0x1E, 0xB9, 0x00, 0xEA, 0x07, 0x06, 0x16, 0x0F, 0x0E, 0x2A, 0x00]
        + [0xF4, 0x01, 0xF5, 0x01, 0xF6, 0x01, 0xF7, 0x01, 0xF8, 0x01, 0xF9, 0x01]
        + [0x48, 0x00]
        + [0x58, 0x02, 0x59, 0x02, 0x5A, 0x02, 0x5B, 0x02, 0x5C, 0x02, 0x5D, 0x02]
    )


def test_realtime_with_high_band():
    m = p.parse_realtime_measurement(_realtime_payload())
    assert m is not None and m.raw_length == 38
    assert m.weight_kg == pytest.approx(77.32)
    assert m.fat_percent == pytest.approx(18.5)
    assert m.heart_rate == 72
    assert m.low_impedance == (500, 501, 502, 503, 504, 505)
    assert m.high_impedance == (600, 601, 602, 603, 604, 605)
    assert m.has_high_frequency is True


def test_realtime_tolerates_bad_timestamp_but_history_does_not():
    bad = bytearray(_realtime_payload())
    bad[4:7] = b"\x00\x00\x00"
    assert p.parse_realtime_measurement(bytes(bad)).timestamp is None
    assert p.parse_history_measurement(bytes(bad) + b"\x01\x00") is None


def test_history_record_flags():
    done = p.parse_history_measurement(_realtime_payload() + b"\x01\x00")
    more = p.parse_history_measurement(_realtime_payload() + b"\x01\x01")
    assert done.is_history_complete is True and more.is_history_complete is False
    assert done.timestamp == datetime(2026, 6, 22, 15, 14, 42)


def test_wrong_lengths_rejected():
    assert p.parse_realtime_measurement(bytes(25)) is None
    assert p.parse_history_measurement(bytes(26)) is None


def test_manager_info_basic_and_extended():
    huid = b"HUID-123".ljust(30, b"\x00")
    dev = b"DEV-9".ljust(40, b"\x00")
    assert p.parse_manager_info(huid + dev).huid == "HUID-123"
    info = p.parse_manager_info(huid + dev + bytes([4]) + b"acct")
    assert info.device_id == "DEV-9" and info.account_info == "acct"
    assert p.parse_manager_info(b"") == p.ManagerInfo("", "", None)
    assert p.parse_manager_info(b"x" * 30) is None


def test_scale_version_bitfield():
    bits = "0010" "0000" "0000" + format(70, "010b") + "00"
    raw = int(bits, 2).to_bytes(3, "big")
    v = p.parse_scale_version(b"\x00" + raw + b"SN12345")
    assert v.version == "2.0.0.70" and v.serial == "SN12345"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("HUAWEI Scale 3", True),
        ("HEM-B19", True),
        ("HUAWEI Scale 3 Pro", False),
        ("Hagrid-B29", False),
        ("HUAWEI Scale 2 Pro", False),
        ("Mi Body Composition Scale", False),
        (None, False),
    ],
)
def test_identify_scale_3(name, expected):
    assert p.identify_scale_3(name) is expected


def test_weight_unit_payload_roundtrips_through_parse():
    assert p.parse_weight_unit(p.weight_unit_payload(metric=True)) == p.WeightUnit(1, "kg")
    assert p.parse_weight_unit(p.weight_unit_payload(metric=False)) == p.WeightUnit(2, "lb")


def test_parse_weight_unit_edge_cases():
    assert p.parse_weight_unit(b"") is None
    assert p.parse_weight_unit(bytes([3])) == p.WeightUnit(3, "g")
    assert p.parse_weight_unit(bytes([99])) == p.WeightUnit(99, "unknown")


def test_max_payload_matches_the_decompiled_apps_chunk_ceiling():
    assert p.MAX_SIMPLE_PAYLOAD_BYTES == 238
    p.build_write_frames(bytes(238))
    with pytest.raises(ValueError):
        p.build_write_frames(bytes(239))


# ---------------------------------------------------------------------------
# Scale 3 Pro / family specific behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("HUAWEI Scale 3 Pro", p.Family.SCALE_3_PRO),
        ("HUAWEI SCALE 3 PRO", p.Family.SCALE_3_PRO),
        ("HAGRID-B29", p.Family.SCALE_3_PRO),
        ("HAGRID2021-B19", p.Family.SCALE_3_PRO),
        ("HUAWEI Scale 3", p.Family.SCALE_3),
        ("HEM-B19", p.Family.SCALE_3),
        ("HUAWEI Scale 2 Pro", p.Family.SCALE_2_PRO),
        ("HAG-B19", p.Family.SCALE_2_PRO),
        ("HONOR Scale 2", p.Family.HONOR_SCALE_2),
    ],
)
def test_identify_hagrid_families(name, family):
    profile = p.identify_hagrid(name)
    assert profile is not None
    assert profile.family == family


def test_scale_3_pro_is_not_mistaken_for_scale_3():
    """The Pro's name contains the Scale 3 marker; the longest match must win."""
    profile = p.identify_hagrid("HUAWEI Scale 3 Pro")
    assert profile.family == p.Family.SCALE_3_PRO
    assert profile.display_name == "HUAWEI Scale 3 Pro"
    assert profile.has_handle is True
    assert profile.expected_high_frequency_impedance is True
    assert profile.needs_tolerant_parsing is True


def test_identify_by_manufacturer_data_and_product_id():
    assert p.identify_hagrid("x", {0x027D: b"model=M00F"}).family == p.Family.SCALE_3_PRO
    assert p.identify_hagrid("x", {0x027D: b"model=M00D"}).family == p.Family.SCALE_3
    assert p.identify_hagrid("x", {0x027D: b"model=007B"}).family == p.Family.SCALE_2_PRO
    assert p.identify_hagrid("Mi Body Composition Scale") is None
    assert p.identify_hagrid(None) is None


def test_gender_encoding_per_model():
    """openScale: only the plain Scale 3 uses male=1; the Pro uses male=0."""
    assert (p.profile_for_family(p.Family.SCALE_3).gender_male,
            p.profile_for_family(p.Family.SCALE_3).gender_female) == (1, 0)
    assert (p.profile_for_family(p.Family.SCALE_3_PRO).gender_male,
            p.profile_for_family(p.Family.SCALE_3_PRO).gender_female) == (0, 1)
    assert (p.profile_for_family(p.Family.SCALE_2_PRO).gender_male,
            p.profile_for_family(p.Family.SCALE_2_PRO).gender_female) == (0, 1)


@pytest.mark.parametrize(
    ("raw", "mode", "expected"),
    [
        (3500, "auto", 350.0),      # Scale 3: 0.1-ohm units
        (3500, "tenths", 350.0),
        (3500, "ohms", 3500.0),
        (350, "auto", 350.0),       # a firmware reporting plain ohm
        (0, "auto", None),
        (60000, "auto", None),      # implausible
    ],
)
def test_impedance_conversion(raw, mode, expected):
    assert p.impedance_to_ohm(raw, mode) == expected


def test_low_and_high_frequency_accessors():
    m = p.parse_realtime_measurement(_realtime_payload())
    assert m.low_ohm_all() == tuple(float(v) for v in range(500, 506))
    assert m.high_ohm_all() == tuple(float(v) for v in range(600, 606))


def test_tolerant_parsing_accepts_unknown_lengths():
    payload = _realtime_payload() + bytes(range(6))  # 44 bytes, tail unknown
    assert p.parse_realtime_measurement(payload) is None
    tolerant = p.parse_realtime_measurement(payload, tolerant=True)
    assert tolerant is not None
    assert tolerant.weight_kg == pytest.approx(77.32)
    assert tolerant.trailing_hex == bytes(range(6)).hex().upper()
    assert tolerant.raw_payload == payload


def test_standard_weight_measurement():
    payload = bytes([0x00]) + round(86.25 / 0.005).to_bytes(2, "little")
    m = p.parse_standard_weight_measurement(payload)
    assert m is not None
    assert m.weight_kg == pytest.approx(86.25, abs=0.01)
    assert m.source == "standard_weight"
    assert m.fat_percent == 0.0
    assert p.parse_standard_weight_measurement(b"") is None


def test_standard_body_composition_measurement():
    # flags: fat present (always), timestamp, impedance, weight
    flags = 0x0002 | 0x0200 | 0x0400
    payload = bytearray(flags.to_bytes(2, "little"))
    payload += round(30.1 * 10).to_bytes(2, "little")       # body fat 30.1 %
    payload += bytes([0xEA, 0x07, 8, 9, 23, 39, 29])        # 2026-08-09 23:39:29
    payload += round(362.5 * 10).to_bytes(2, "little")      # impedance
    payload += round(86.25 / 0.005).to_bytes(2, "little")   # weight
    m = p.parse_standard_body_composition_measurement(bytes(payload))
    assert m is not None
    assert m.fat_percent == pytest.approx(30.1)
    assert m.weight_kg == pytest.approx(86.25, abs=0.01)
    assert m.impedance_ohm() == pytest.approx(362.5)
    assert m.timestamp == datetime(2026, 8, 9, 23, 39, 29)


def test_capability_bits():
    # bit 18 set -> byte 2, bit 2 (0x04); also bit 0 -> byte 0, 0x01
    assert p.parse_capability_bits("010004") == {0, 18}
    assert p.CAPABILITY_BIT_HIGH_FREQUENCY_IMPEDANCE in p.parse_capability_bits("010004")
    assert p.parse_capability_bits("zzz") == set()
    assert p.find_capability_bits("x", {0x027D: b"capacity:040100"}) == {2, 8}
