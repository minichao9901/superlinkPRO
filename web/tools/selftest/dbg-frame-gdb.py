"""Run under GDB's embedded Python; input supplied by dbg-frame-gdb-oracle.mjs."""
try:
    import gdb
except ImportError:
    # Some cross-GDB packages ship the extension but omit gdb/__init__.py.
    # The extension itself exposes the complete API used below.
    import _gdb as gdb

try:
    _string_types = (basestring,)
    _integer_types = (int, long)
    _PY2 = True
except NameError:
    _string_types = (str,)
    _integer_types = (int,)
    _PY2 = False


def json_string(value):
    if not isinstance(value, _string_types):
        value = str(value)
    out = [u'"']
    for char in value:
        code = ord(char)
        if char == u'"': out.append(u'\\"')
        elif char == u'\\': out.append(u'\\\\')
        elif code == 8: out.append(u'\\b')
        elif code == 9: out.append(u'\\t')
        elif code == 10: out.append(u'\\n')
        elif code == 12: out.append(u'\\f')
        elif code == 13: out.append(u'\\r')
        elif code < 32 or code > 126: out.append(u'\\u%04x' % code)
        else: out.append(char)
    out.append(u'"')
    return u''.join(out)


def json_dumps(value):
    if value is None: return u'null'
    if value is True: return u'true'
    if value is False: return u'false'
    if isinstance(value, _string_types): return json_string(value)
    if isinstance(value, _integer_types): return str(value)
    if isinstance(value, float): return repr(value)
    if isinstance(value, (list, tuple)):
        return u'[' + u','.join(json_dumps(item) for item in value) + u']'
    if isinstance(value, dict):
        return u'{' + u','.join(json_string(key) + u':' + json_dumps(item)
                                 for key, item in value.items()) + u'}'
    raise TypeError('unsupported JSON value ' + str(type(value)))


def write_json(path, value):
    encoded = json_dumps(value)
    # json_string escapes all non-ASCII codepoints, so the file is ASCII.
    # Avoid codecs.lookup() here: stripped Python 2 GDB builds may have no
    # codec registry even though their built-in JSON-free Python API works.
    if _PY2:
        encoded = ''.join(chr(ord(char)) for char in encoded)
    else:
        encoded = encoded.encode('ascii')
    with open(str(path), 'wb') as output:
        output.write(encoded)


def to_bytes(value):
    if hasattr(value, 'tobytes'):
        return value.tobytes()
    return bytes(value)


def from_hex(text):
    try:
        return bytes.fromhex(text)
    except AttributeError:
        return ''.join(chr(int(text[i:i + 2], 16)) for i in range(0, len(text), 2))


def byte_value(value):
    return value if isinstance(value, int) else ord(value)


def u32le(value):
    return sum(byte_value(value[i]) << (i * 8) for i in range(4))


def hardware_breakpoint(address):
    # Newer GDB versions expose BP_HARDWARE_BREAKPOINT. GDB 10.x does not,
    # so request and delete a hardware breakpoint through the GDB command API.
    hardware_type = getattr(gdb, 'BP_HARDWARE_BREAKPOINT', None)
    if hardware_type is not None:
        return gdb.Breakpoint('*' + str(address), type=hardware_type, internal=True)
    gdb.execute('hbreak *' + str(address))
    return None


def delete_hardware_breakpoint(bp):
    if bp is None:
        # The collector owns a fresh batch GDB instance with no user breakpoints.
        gdb.execute('delete')
    else:
        bp.delete()


def scalar_fields(value, path="", depth=0):
    if depth > 4:
        raise RuntimeError("oracle type nesting limit")
    typ = value.type.strip_typedefs()
    if typ.code == gdb.TYPE_CODE_ARRAY:
        lo, hi = typ.range()
        if hi - lo + 1 > 16:
            raise RuntimeError("oracle array limit")
        out = []
        for i in range(lo, hi + 1):
            out += scalar_fields(value[i], path + "[" + str(i) + "]", depth + 1)
        return out
    if typ.code in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
        out = []
        for field in typ.fields():
            if not field.name:
                raise RuntimeError("anonymous oracle member")
            out += scalar_fields(value[field], (path + "." if path else "") + field.name, depth + 1)
        return out
    if typ.code not in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_ENUM, gdb.TYPE_CODE_PTR, gdb.TYPE_CODE_BOOL):
        raise RuntimeError("unsupported oracle scalar " + str(typ))
    return [{"path": path, "value": str(int(value))}]


def variables(frame):
    blocks = []
    block = frame.block()
    while block is not None and not block.is_global and not block.is_static:
        blocks.append(block)
        block = block.superblock
    result, occurrences = [], {}
    # Outer first matches lexical DIE traversal; do not collapse shadowed variables.
    for block in reversed(blocks):
        for symbol in block:
            if not (symbol.is_argument or symbol.is_variable):
                continue
            name = symbol.name
            occurrence = occurrences.get(name, 0)
            occurrences[name] = occurrence + 1
            item = {"name": name, "occurrence": occurrence, "argument": bool(symbol.is_argument), "status": "ok", "fields": []}
            try:
                value = frame.read_var(symbol)
                if value.is_optimized_out:
                    item["status"] = "unavailable"
                    item["reason"] = "optimized out"
                else:
                    item["fields"] = scalar_fields(value)
                    if value.address is not None:
                        item["address"] = int(value.address)
                if frame.name() == "engine_frame_migrate" and name == "arg":
                    # Value.address is absent for valid DWARF stack-value
                    # expressions. Preserve GDB's location ranges so the
                    # contract can verify the location active at each PC.
                    item["location"] = gdb.execute("info address " + name, False, True)
            except gdb.error as error:
                item["status"] = "unavailable"
                item["reason"] = str(error)
            result.append(item)
    return result


def capture():
    frames = []
    frame = gdb.newest_frame()
    for _ in range(32):
        if frame is None:
            break
        name = frame.name() or ""
        if not name.startswith("engine_frame_"):
            break
        frame_vars = variables(frame)
        frames.append({"name": name, "pc": int(frame.pc()) & ~1,
                       "sp": int(frame.read_register("sp")), "variables": frame_vars})
        frame = frame.older()
    return frames


def main():
    config = CONFIG
    result = {"schema": 1, "board": config["board"], "build": config["build"],
              "elfSha256": config["elfSha256"], "tool": "gdb " + gdb.VERSION,
              "codeVerified": False, "cases": {}}
    try:
        gdb.execute("set pagination off")
        gdb.execute("set confirm off")
        gdb.execute("set remotetimeout 15")
        gdb.execute("set tcp connect-timeout 15")
        gdb.execute("target extended-remote " + config["remote"])
        gdb.execute("monitor reset halt")
        if config["board"] == "f103ze":
            vector = to_bytes(gdb.selected_inferior().read_memory(0x08000000, 8))
            gdb.execute("monitor mww 0xe000ed08 0x08000000")
            gdb.execute("set $sp = " + str(u32le(vector[:4])))
            gdb.execute("set $pc = " + str(u32le(vector[4:]) & ~1))
            gdb.execute("set $primask = 0")
            gdb.execute("set $faultmask = 0")
        inferior = gdb.selected_inferior()
        for section in config["code"]:
            expected = from_hex(section["hex"])
            for offset in range(0, len(expected), 4096):
                chunk = expected[offset:offset + 4096]
                actual = to_bytes(inferior.read_memory(section["addr"] + offset, len(chunk)))
                if actual != chunk:
                    raise RuntimeError("target code differs from ELF: " + section["name"])
        result["codeVerified"] = True
        for case in config["cases"]:
            bp = hardware_breakpoint(case["address"])
            try:
                gdb.execute("continue")
                pc = int(gdb.parse_and_eval("$pc")) & ~1
                if pc != case["address"]:
                    raise RuntimeError("stopped outside checkpoint " + case["id"])
                result["cases"][case["id"]] = {"pc": pc, "frames": capture()}
            finally:
                delete_hardware_breakpoint(bp)
        # Detach releases the GDB client; stop the GDB server before WebUSB claims the probe.
        gdb.execute("detach")
        write_json(config["out"], result)
    except Exception as error:
        gdb.write("FRAME_ORACLE_ERROR: " + str(error) + "\n", gdb.STDERR)
        try:
            gdb.execute("disconnect")
        except gdb.error:
            pass
        gdb.execute("quit 1")

main()
