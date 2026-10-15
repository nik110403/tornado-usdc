from lib import keccak


def test_empty_string():
    assert keccak.keccak256(b"") == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


def test_transfer_topic():
    assert keccak.topic("Transfer(address,address,uint256)") == (
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )


def test_known_cross_checks():
    assert keccak.topic("Blacklisted(address)") == (
        "0xffa4e6181777692565cf28528fc88fd1516ea86b56da075235fa575af6a4b855"
    )
    assert keccak.topic("EncryptedNote(address,bytes)") == (
        "0xfa28df43db3553771f7209dcef046f3bdfea15870ab625dcda30ac58b82b4008"
    )


def test_balance_of_selector():
    assert keccak.selector("balanceOf(address)") == "0x70a08231"
