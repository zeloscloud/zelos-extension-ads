"""Symbol and datatype table parsing, flattening, naming, filters, values, the map."""

from __future__ import annotations

import pytest
import zelos_sdk

from zelos_extension_ads import symbols as sym
from zelos_extension_ads.demo.plc import DemoPlc, encode_symbol, encode_type


def _fields() -> list[sym.Field]:
    plc = DemoPlc()
    plc.stop()
    return sym.flatten(sym.parse_symbols(plc.symbol_table()), sym.parse_types(plc.type_table()))


def test_tables_round_trip_and_flatten():
    """Structs and arrays become scalar leaves at the right offsets; pointers vanish."""
    fields = _fields()
    by = {f.symbol: f for f in fields}
    assert "MAIN.pBuffer" not in by
    motor = by["MAIN.stMotor.nRpm"]
    assert (motor.index_offset - by["MAIN.stMotor.fSpeed"].index_offset, motor.size) == (8, 2)
    assert motor.data_type == sym.ADST_INT16 and motor.datatype == zelos_sdk.DataType.Int16
    temps = [by[f"MAIN.arrTemps[{i}]"] for i in range(4)]
    assert [t.index_offset - temps[0].index_offset for t in temps] == [0, 4, 8, 12]
    assert by["MAIN.bOut1"].comment == "demo output 1"
    # The real table carries extras (GUIDs, attributes) inside entryLength: a
    # padded entry parses the same.
    raw = sym.SymbolEntry("MAIN.nCycle", "UDINT", 0x4040, 0, 4, sym.ADST_UINT32, 8, "cycles")
    entry = sym.parse_symbols(encode_symbol(raw) + b"\0" * 8)
    assert len(entry) == 1 and entry[0] == raw


def test_multidimensional_array_and_alias():
    types = {
        "ARRAY [1..2, 0..1] OF INT": sym.TypeEntry(
            "ARRAY [1..2, 0..1] OF INT", "INT", 8, 0, sym.ADST_INT16, 0, [(1, 2), (0, 2)]
        ),
        "OTCID": sym.TypeEntry("OTCID", "UDINT", 4, 0, sym.ADST_UINT32, 0),
    }
    table = encode_type(types["ARRAY [1..2, 0..1] OF INT"]) + encode_type(types["OTCID"])
    assert sym.parse_types(table).keys() == types.keys()
    entries = [
        sym.SymbolEntry(
            "GVL.arr", "ARRAY [1..2, 0..1] OF INT", 0x4040, 100, 8, sym.ADST_BIGTYPE, 0
        ),
        sym.SymbolEntry("GVL.oid", "OTCID", 0x4040, 200, 4, sym.ADST_UINT32, 0),
    ]
    fields = sym.flatten(entries, types)
    assert [(f.symbol, f.index_offset) for f in fields] == [
        ("GVL.arr[1,0]", 100),
        ("GVL.arr[1,1]", 102),
        ("GVL.arr[2,0]", 104),
        ("GVL.arr[2,1]", 106),
        ("GVL.oid", 200),
    ]
    assert fields[0].name == "arr_1_0"


def test_bit_struct_members():
    """BITVALUES members carry offset and size in bits; STATIC and PROPITEM hold no data."""
    bit = sym.FLAG_BITVALUES | 0x2
    members = [sym.TypeEntry(f"b{i}", "BIT", 1, i, sym.ADST_BIT, bit) for i in range(3)]
    members += [
        sym.TypeEntry("w", "WORD", 2, 2, sym.ADST_UINT16, 0x2),
        sym.TypeEntry("s", "WORD", 2, 0, sym.ADST_UINT16, sym.FLAG_STATIC),
        sym.TypeEntry("p", "WORD", 2, 0, sym.ADST_UINT16, sym.FLAG_PROPITEM),
    ]
    types = {"ST_Flags": sym.TypeEntry("ST_Flags", "", 4, 0, sym.ADST_BIGTYPE, 1, [], members)}
    entry = sym.SymbolEntry("MAIN.st", "ST_Flags", 0x4040, 100, 4, sym.ADST_BIGTYPE, 8)
    mem = bytes([0b101, 0, 0x34, 0x12])
    # One block spans the entry's leaves (gap at byte 1 included), each decoded out of it.
    (block,), rest = sym.blocks(sym.flatten([entry], types), 1024)
    assert (block.start, block.size, rest) == (100, 4, [])
    got = {f.symbol: v for f, v in block.values(mem)}
    assert got == {"MAIN.st.b0": True, "MAIN.st.b1": False, "MAIN.st.b2": True, "MAIN.st.w": 0x1234}
    # A span over the limit is not subscribed: its leaves come back to be polled.
    assert sym.blocks(block.fields, 3) == ([], block.fields)
    assert sym.decode("ä".encode("cp1252"), sym.ADST_STRING) == "ä"


def test_trace_names_and_layout():
    assert sym.trace_names("MAIN.stMotor.fSpeed") == ("MAIN", "stMotor_fSpeed")
    assert sym.trace_names("GVL_IO.arr[3].x") == ("GVL_IO", "arr_3_x")
    fields = _fields()
    events, dropped = sym.layout(fields)
    assert set(events) == {"MAIN", "GVL_IO", "TwinCAT_SystemInfoVarList"} and not dropped
    # A second symbol folding to a taken name is skipped, never re-pointed.
    clash = sym.Field(
        "MAIN.stMotor_fSpeed", "MAIN", "stMotor_fSpeed", 0, 0, 8, sym.ADST_REAL64, "LREAL"
    )
    _, dropped = sym.layout([*fields, clash])
    assert dropped == ["MAIN.stMotor_fSpeed"]


@pytest.mark.parametrize(
    ("include", "exclude", "kept"),
    [
        ((), (), ["MAIN.nCycle", "GVL_IO.fFlow"]),  # system globals out by default
        (("MAIN.*",), (), ["MAIN.nCycle"]),
        (("**.fFlow",), (), ["GVL_IO.fFlow"]),
        ((), ("GVL_IO.**",), ["MAIN.nCycle"]),
        (("TwinCAT_SystemInfoVarList.**",), (), ["TwinCAT_SystemInfoVarList._AppInfo.AppName"]),
        (("MAIN.*",), ("MAIN.nCycle",), []),
    ],
)
def test_filters(include, exclude, kept):
    probe = ["MAIN.nCycle", "GVL_IO.fFlow", "TwinCAT_SystemInfoVarList._AppInfo.AppName"]
    f = sym.SymbolFilter.parse(include, exclude)
    assert [s for s in probe if f.keeps(s)] == kept


def test_invalid_pattern():
    with pytest.raises(ValueError):
        sym.SymbolFilter.parse(["MAIN..x"])


def test_values_round_trip():
    for dtype, value, size in [
        (sym.ADST_BIT, True, 1),
        (sym.ADST_INT16, -7, 2),
        (sym.ADST_UINT32, 4_000_000_000, 4),
        (sym.ADST_REAL64, 1.5, 8),
        (sym.ADST_STRING, "RUN", 8),
        (sym.ADST_WSTRING, "ok", 10),
    ]:
        data = sym.encode(value, dtype, size)
        assert len(data) == size and sym.decode(data, dtype) == value
    assert sym.decode(b"\x01", sym.ADST_REAL32) is None
    assert sym.decode(b"\x02\x00", sym.ADST_INT16, scale=0.5) == 1.0
    assert sym.coerce_text("yes", sym.ADST_BIT) is True
    assert sym.coerce_text("0x10", sym.ADST_UINT8) == 16
    for text, dtype in [("maybe", sym.ADST_BIT), ("300", sym.ADST_UINT8), ("x", sym.ADST_REAL32)]:
        with pytest.raises(ValueError):
            sym.coerce_text(text, dtype)


def test_symbol_map(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(
        '{"events": {"motor": [{"symbol": "main.stmotor.fspeed", "name": "speed", "unit": "rpm", '
        '"scale": 2.0}, {"symbol": "MAIN.nCycle"}], "io": [{"symbol": "GVL_IO.nMissing"}]}}'
    )
    symbol_map = sym.SymbolMap.from_file(path)
    fields, missing = symbol_map.apply(_fields())
    assert missing == ["GVL_IO.nMissing"]
    assert [(f.event, f.name, f.unit, f.scale) for f in fields] == [
        ("motor", "speed", "rpm", 2.0),
        ("motor", "nCycle", "", 1.0),
    ]
    assert fields[0].symbol == "MAIN.stMotor.fSpeed"  # the target's own case
    for bad in (
        {"events": {"e": [{"symbol": "A.b"}, {"symbol": "A.c", "name": "b"}]}},
        {"events": {"e": [{}]}},
    ):
        with pytest.raises(ValueError):
            sym.SymbolMap.from_dict(bad)
