"""Converts a Naira amount into words, matching Richardson Oil & Gas's
existing invoice format (e.g. "TWENTY MILLION, EIGHT HUNDRED AND FORTY
THOUSAND, SIX HUNDRED AND FIFTY-THREE NAIRA, FIVE KOBO ONLY").
"""

from decimal import ROUND_HALF_UP, Decimal

_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]
_SCALES = [(1_000_000_000, "Billion"), (1_000_000, "Million"), (1_000, "Thousand")]


def _three_digit_words(n: int) -> str:
    words = []
    if n >= 100:
        words.append(_ONES[n // 100])
        words.append("Hundred")
        n %= 100
        if n:
            words.append("and")
    if n >= 20:
        tens_word = _TENS[n // 10]
        ones = n % 10
        words.append(f"{tens_word}-{_ONES[ones]}" if ones else tens_word)
    elif n > 0:
        words.append(_ONES[n])
    return " ".join(words)


def number_to_words(n: int) -> str:
    if n == 0:
        return "Zero"
    parts = []
    remaining = n
    for value, name in _SCALES:
        if remaining >= value:
            count = remaining // value
            parts.append(f"{_three_digit_words(count)} {name}")
            remaining %= value
    if remaining:
        parts.append(_three_digit_words(remaining))
    return ", ".join(parts)


def amount_in_words_naira(amount) -> str:
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    naira = int(value)
    kobo = int((value - naira) * 100)
    result = f"{number_to_words(naira).upper()} NAIRA"
    if kobo:
        result += f", {number_to_words(kobo).upper()} KOBO"
    return result + " ONLY"
