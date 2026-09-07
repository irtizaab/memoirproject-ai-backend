"""The one password this product has.

No database. These are about the function itself, and about the ways a
verification can be talked into saying yes when it should say no — which is the
only kind of bug in a file like this that matters.
"""

import pytest

from src.domain.memoirs.passphrase import (
    MINIMUM_LENGTH,
    PassphraseTooShort,
    hash_passphrase,
    needs_rehash,
    verify_passphrase,
)


def test_the_right_passphrase_verifies():
    assert verify_passphrase("ellsworth lane", hash_passphrase("ellsworth lane"))


def test_a_wrong_passphrase_does_not():
    assert not verify_passphrase("ellsworth road", hash_passphrase("ellsworth lane"))


def test_case_is_part_of_the_passphrase():
    """Normalising case would quietly halve a space people already choose badly."""
    assert not verify_passphrase("Ellsworth Lane", hash_passphrase("ellsworth lane"))


def test_surrounding_whitespace_is_forgiven():
    """A passphrase read off paper and pasted with a trailing space still opens.

    Stripped on both sides of the comparison, so the forgiveness is symmetric —
    which is the only way it is not a second, weaker passphrase.
    """
    stored = hash_passphrase("  ellsworth lane  ")
    assert verify_passphrase("ellsworth lane", stored)
    assert verify_passphrase("  ellsworth lane\n", stored)


def test_the_stored_value_does_not_contain_the_passphrase():
    assert "ellsworth" not in hash_passphrase("ellsworth lane")


def test_two_memoirs_with_the_same_passphrase_store_different_values():
    """Per-value salt. Without it, one cracked hash opens every memoir using it,
    and a rainbow table works on all of them at once."""
    assert hash_passphrase("ellsworth lane") != hash_passphrase("ellsworth lane")


def test_the_parameters_travel_with_the_value():
    """So the work factor can be raised later without invalidating every row."""
    assert hash_passphrase("ellsworth lane").startswith("scrypt$16384$8$1$")


def test_a_short_passphrase_is_refused():
    with pytest.raises(PassphraseTooShort):
        hash_passphrase("a" * (MINIMUM_LENGTH - 1))


def test_whitespace_is_not_length():
    """Ten spaces is a password of nothing, and this is the last check before
    it becomes one."""
    with pytest.raises(PassphraseTooShort):
        hash_passphrase("          ")


# ---------------------------------------------------------------------------
# The ways verification must refuse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        None,  # a memoir with no passphrase: not "open to everyone"
        "",
        "scrypt$16384$8$1$only-four-parts",
        "argon2$16384$8$1$c2FsdA==$a2V5",  # an algorithm this code does not know
        "scrypt$notanumber$8$1$c2FsdA==$a2V5",
        "not a hash at all",
    ],
)
def test_a_value_that_cannot_be_parsed_never_opens_anything(stored):
    """Malformed must fail closed.

    The bug this forbids is the one worth having a test for: an exception path
    or a falsy check that lets a caller in *because* something was broken.
    """
    assert not verify_passphrase("ellsworth lane", stored)


def test_an_empty_attempt_never_opens_anything():
    assert not verify_passphrase("", hash_passphrase("ellsworth lane"))


def test_a_value_written_with_todays_parameters_is_not_rehashed():
    assert not needs_rehash(hash_passphrase("ellsworth lane"))


def test_a_value_written_with_weaker_parameters_is():
    """The open path rewrites these at the next successful use, which is the
    one moment the plaintext is in hand."""
    assert needs_rehash("scrypt$1024$8$1$c2FsdA==$a2V5")


def test_no_passphrase_is_not_a_stale_one():
    """`needs_rehash` answering yes on a null would ask the open path to
    rewrite a memoir that has no passphrase at all."""
    assert not needs_rehash(None)
