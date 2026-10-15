"""Pure-Python Keccak-256 (the legacy Ethereum variant — pad byte 0x01, not
the NIST SHA3 0x06). No third-party dependency, so the hashes used to build
event topics/selectors are auditable line by line instead of trusted from a
compiled extension.

Self-tests run at import time (see bottom of file) against two known-good
vectors. If they fail, importing this module raises immediately — nothing
downstream may use an unverified topic hash.
"""

_MASK64 = (1 << 64) - 1

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

# rotation offsets r[x][y], per the Keccak spec
_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]

_RATE_BYTES = 136  # 1088 bits, for the 256-bit-output / 512-bit-capacity variant


def _rol(x, n):
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _MASK64


def _keccak_f(state):
    """In-place Keccak-f[1600] permutation on a 5x5 array of 64-bit lanes
    (state[x][y])."""
    for rc in _RC:
        # theta
        c = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= d[x]
        # rho + pi
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(state[x][y], _ROT[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                state[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y] & _MASK64)
        # iota
        state[0][0] ^= rc
    return state


def keccak256_bytes(data: bytes) -> bytes:
    """Raw 32-byte Keccak-256 digest."""
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % _RATE_BYTES != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    state = [[0] * 5 for _ in range(5)]
    for i in range(0, len(padded), _RATE_BYTES):
        block = padded[i:i + _RATE_BYTES]
        for j in range(_RATE_BYTES // 8):
            lane = int.from_bytes(block[j * 8:j * 8 + 8], "little")
            x, y = j % 5, j // 5
            state[x][y] ^= lane
        _keccak_f(state)

    out = bytearray()
    for i in range(4):  # 4 lanes = 32 bytes
        x, y = i % 5, i // 5
        out += state[x][y].to_bytes(8, "little")
    return bytes(out)


def keccak256(data: bytes) -> str:
    """Hex digest (no 0x prefix), lowercase."""
    return keccak256_bytes(data).hex()


def topic(signature: str) -> str:
    """0x-prefixed 32-byte event topic0 for an ABI signature string, e.g.
    'Transfer(address,address,uint256)'."""
    return "0x" + keccak256(signature.encode("ascii"))


def selector(signature: str) -> str:
    """0x-prefixed 4-byte function selector for an ABI signature string."""
    return "0x" + keccak256(signature.encode("ascii"))[:8]


def _self_test():
    empty = keccak256(b"")
    assert empty == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470", (
        f"keccak256('') self-test FAILED: got {empty}"
    )
    xfer = keccak256(b"Transfer(address,address,uint256)")
    assert xfer == "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", (
        f"keccak256('Transfer(...)') self-test FAILED: got {xfer}"
    )


_self_test()
