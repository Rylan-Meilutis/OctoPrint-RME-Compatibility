import hashlib


def bbf(version=(6, 5, 7), identity=(7, 1, 0), payload=b"application"):
    header = bytearray(576)
    header[96:100] = len(payload).to_bytes(4, "little")
    header[100:103] = bytes(version)
    header[111] = identity[0]
    header[112] = 2
    header[113] = identity[2]
    header[114] = identity[1]
    data = header + payload
    data[64:96] = hashlib.sha256(data[96:]).digest()
    return bytes(data)
