"""
Money arithmetic.

Every amount in this system is an integer number of pesewas. GHS 12.50 is
1250. There are no floats and no Decimals in the database.

Why this file exists at all: the commission split is the one calculation that
decides what a professional is paid, and "just multiply by 0.15" is wrong in a
way that only shows up in aggregate. These functions are small enough to read
in full and are tested against the awkward cases.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Split:
    """The result of dividing a held amount between professional and ONA."""

    total_pesewas: int
    professional_pesewas: int
    commission_pesewas: int
    commission_percent: int

    def __post_init__(self):
        if self.professional_pesewas + self.commission_pesewas != self.total_pesewas:
            raise ValueError(
                'Split does not reconcile: %d + %d != %d'
                % (self.professional_pesewas, self.commission_pesewas, self.total_pesewas)
            )


def split_commission(total_pesewas: int, commission_percent: int) -> Split:
    """
    Divide a held amount between the professional and ONA.

    The rule, from docs/spec.md: commission is rounded DOWN, and the remainder
    goes to the professional.

    Rounding down the commission rather than the payout means any half-pesewa
    goes to the person who did the work, not to the platform. Over thousands
    of transactions that is a small amount of money and a large amount of
    trust. It also guarantees the two parts always sum to the total, so the
    ledger cannot drift.

        >>> split_commission(125000, 15)
        Split(total_pesewas=125000, professional_pesewas=106250,
              commission_pesewas=18750, commission_percent=15)

        >>> s = split_commission(101, 15)   # 15% of 101 is 15.15
        >>> s.commission_pesewas            # rounded down
        15
        >>> s.professional_pesewas          # keeps the remainder
        86
    """
    if type(total_pesewas) is not int:
        raise TypeError(
            'total_pesewas must be int, got %s. Money is never float here.'
            % type(total_pesewas).__name__
        )
    if total_pesewas < 0:
        raise ValueError('total_pesewas must not be negative.')
    if type(commission_percent) is not int:
        raise TypeError(
            'commission_percent must be int, got %s.'
            % type(commission_percent).__name__
        )
    if not 0 <= commission_percent <= 100:
        raise ValueError('commission_percent must be between 0 and 100.')

    # Integer floor division: no float ever enters the calculation.
    commission = (total_pesewas * commission_percent) // 100
    professional = total_pesewas - commission

    return Split(
        total_pesewas=total_pesewas,
        professional_pesewas=professional,
        commission_pesewas=commission,
        commission_percent=commission_percent,
    )


def format_ghs(pesewas: int) -> str:
    """Format for display: 125000 -> 'GHS 1,250.00'."""
    if type(pesewas) is not int:
        raise TypeError('pesewas must be int, got %s.' % type(pesewas).__name__)
    negative = pesewas < 0
    whole, part = divmod(abs(pesewas), 100)
    return '%sGHS %s.%02d' % ('-' if negative else '', f'{whole:,}', part)
