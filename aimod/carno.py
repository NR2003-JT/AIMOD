"""Car numbers as NR2003 stores them, shown the way they're painted on the car.

NR2003 keeps a car number as an int. A number written with leading zeros in the .car file ("01", "007") is
stored as (digits x 1000) + value: "01" = 2001, "00" = 2000, "007" = 3007. Some mods write "00" as 100 instead
(the stock 2004 Kenny Wallace cars). AIMOD keeps the raw int as the car's identity (so #01 and #1 stay two cars)
and shows the painted number: 2001 -> "01", 2000 -> "00", 100 -> "00".

CarNo is an int that prints as its label: f"#{n}", str(n) and "%s" % n give "01"; hashing, ==, sorting and
JSON (which writes the int) keep the raw value.
"""
from __future__ import annotations


def label(v) -> str:
    v = int(v)
    if v == 100:
        return "00"
    if v >= 2000:
        digits, n = divmod(v, 1000)
        if 2 <= digits <= 3 and n < 10 ** digits:
            return str(n).zfill(digits)
    return str(v)


class CarNo(int):
    def __str__(self):
        return label(self)

    def __format__(self, spec):
        kind = spec[-1:]
        if kind.isalpha() and kind != "s":
            return int.__format__(int(self), spec)                 # "d", "x"...: the raw number
        return format(label(self), spec.rstrip("s"))


def raw(text: str) -> int:
    """The int NR2003 stores for a number as written in a .car file ("01" -> 2001)."""
    s = str(text).strip().lstrip("#")
    n = int(s)
    if len(s) > 1 and s.startswith("0"):
        return len(s) * 1000 + n
    return n


def matches(number, text: str) -> bool:
    """A typed number ("01", "00", "1") is this car: compared as painted, so "00" finds 2000 and 100."""
    s = str(text).strip().lstrip("#")
    return bool(s) and s.isdigit() and label(number) == s
