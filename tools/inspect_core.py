"""Read-only PE inspection for the installed Weixin core. Never loads the DLL."""
import argparse
import bisect
import hashlib
import json
from pathlib import Path
import re
import struct
import sys

sys.path.insert(0, str(Path(__file__).parent / "python-deps"))
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from pe_helpers import function_ranges


def inspect(path, with_xrefs=True):
    data = Path(path).read_bytes()
    pe = pefile.PE(data=data, fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
    if pe.FILE_HEADER.Machine != 0x8664:
        raise ValueError("Expected Windows x64 core")
    base = pe.OPTIONAL_HEADER.ImageBase
    versions = {}
    for group in getattr(pe, "FileInfo", []):
        for entry in group:
            for table in getattr(entry, "StringTable", []):
                versions.update({k.decode(errors="replace"): v.decode(errors="replace")
                                 for k, v in table.entries.items()
                                 if k in (b"FileVersion", b"ProductVersion")})
    sections = [{"name": s.Name.rstrip(b"\0").decode(), "rva": hex(s.VirtualAddress),
                 "fileOffset": hex(s.PointerToRawData), "size": s.SizeOfRawData,
                 "executable": bool(s.Characteristics & 0x20000000)} for s in pe.sections]
    # Static log strings provide anchors, not callable addresses or proven signatures.
    needles = re.compile(rb"(?:send.?text|send.?msg|send.?message|text.?message|add.?msgs|on.?sync|new.?sync|message_storage|contact_storage)", re.I)
    anchors = []
    for match in re.finditer(rb"[\x20-\x7e]{8,512}", data):
        value = match.group()
        if needles.search(value):
            try:
                rva = pe.get_rva_from_offset(match.start())
            except pefile.PEFormatError:
                continue
            anchors.append({"rva": hex(rva), "text": value.decode(), "references": []})
    functions = function_ranges(pe)
    functions.sort()
    starts = [x[0] for x in functions]
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    if with_xrefs:
        wanted = {int(a["rva"], 16): a for a in anchors}
        # REX + LEA/MOV reg,[RIP+disp32]. Validate each candidate with Capstone.
        pattern = re.compile(rb"[\x48-\x4f][\x8d\x8b][\x05\x0d\x15\x1d\x25\x2d\x35\x3d]....", re.S)
        for section in pe.sections:
            if not section.Characteristics & 0x20000000:
                continue
            code = section.get_data()
            for match in pattern.finditer(code):
                rva = section.VirtualAddress + match.start()
                target = rva + 7 + struct.unpack_from("<i", match.group(), 3)[0]
                if target not in wanted:
                    continue
                instruction = next(md.disasm(match.group(), base + rva, count=1), None)
                if instruction is None or instruction.size != 7:
                    continue
                index = bisect.bisect_right(starts, rva) - 1
                function = functions[index] if index >= 0 and rva < functions[index][1] else None
                wanted[target]["references"].append({"instructionRva": hex(rva),
                    "functionRva": hex(function[0]) if function else None,
                    "functionEndRva": hex(function[1]) if function else None})
    return {"path": str(Path(path).resolve()), "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "machine": "x64", "version": versions, "imageBase": hex(base),
            "sections": sections, "anchors": anchors, "runtimeValidated": False,
            "sendEnabled": False, "warning": "String references are research candidates; ABI, object layout, thread and receipts remain unverified."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dll")
    parser.add_argument("--out", required=True)
    parser.add_argument("--no-xrefs", action="store_true")
    args = parser.parse_args()
    result = inspect(args.dll, not args.no_xrefs)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"version": result["version"], "sha256": result["sha256"],
        "anchors": len(result["anchors"]), "referencedAnchors": sum(bool(a["references"]) for a in result["anchors"]),
        "report": str(target.resolve())}, ensure_ascii=False))
