"""Print one function and static string operands; never executes the target."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent / "python-deps"))
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM, X86_REG_RIP

parser = argparse.ArgumentParser()
parser.add_argument("dll")
parser.add_argument("rva", type=lambda s: int(s, 0))
parser.add_argument("--size", type=lambda s: int(s, 0), default=2048)
args = parser.parse_args()
pe = pefile.PE(args.dll, fast_load=True)
base = pe.OPTIONAL_HEADER.ImageBase
md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True
for ins in md.disasm(pe.get_data(args.rva, args.size), base + args.rva):
    annotation = ""
    for op in ins.operands:
        if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
            target = ins.address + ins.size + op.mem.disp - base
            try:
                raw = pe.get_data(target, 200).split(b"\0", 1)[0]
                if len(raw) >= 5 and all(32 <= c <= 126 for c in raw):
                    annotation = " ; " + repr(raw.decode())
            except pefile.PEFormatError:
                pass
    print(f"{ins.address-base:#010x} {ins.mnemonic:8s} {ins.op_str}{annotation}")
