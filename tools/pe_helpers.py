"""快速只读 PE 函数边界，避免解析无关的异常处理结构。"""
import struct


def function_ranges(pe):
    entry = pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
    raw = pe.get_data(entry.VirtualAddress, entry.Size)
    return [(a, b) for a, b, _ in struct.iter_unpack('<III', raw) if a and b > a]
