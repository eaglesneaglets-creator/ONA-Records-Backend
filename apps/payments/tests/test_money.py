"""
Money arithmetic tests.

These exist because the commission split decides what a professional is paid.
A rounding bug here does not crash anything — it quietly pays the wrong
amount, on every transaction, until someone reconciles by hand.

No Django imports: money.py is deliberately framework-free so it can be
tested in isolation and reasoned about in full.
"""

import random

import pytest

from apps.payments.money import Split, format_ghs, split_commission


class TestSplitCommission:
    def test_matches_the_approved_designs(self):
        """
        The figures shown on the design review screens must hold.

        GHS 1,250 at 15% is the example used throughout the customer approve
        panel, the admin release dialog and the escrow strip. If this changes,
        32 screens are lying.
        """
        s = split_commission(125_000, 15)
        assert s.professional_pesewas == 106_250   # GHS 1,062.50
        assert s.commission_pesewas == 18_750      # GHS   187.50

    def test_parts_always_sum_to_the_total(self):
        """No money is invented or lost, at any amount or rate."""
        for total in (0, 1, 99, 100, 101, 999, 125_000, 999_999_999):
            for pct in (0, 1, 15, 33, 50, 99, 100):
                s = split_commission(total, pct)
                assert s.professional_pesewas + s.commission_pesewas == total

    def test_rounding_favours_the_professional(self):
        """
        Commission rounds DOWN; the remainder goes to the professional.

        15% of 101 pesewas is 15.15. The platform takes 15 and the
        professional keeps 86 — not the other way round. Over thousands of
        transactions this is a small sum and a large amount of trust.
        """
        s = split_commission(101, 15)
        assert s.commission_pesewas == 15
        assert s.professional_pesewas == 86

    @pytest.mark.parametrize('seed', [0, 1, 42])
    def test_invariants_hold_across_random_amounts(self, seed):
        """
        Property test. Fixed seeds so a failure is reproducible rather than
        appearing once in CI and never again.
        """
        rng = random.Random(seed)
        for _ in range(5_000):
            total = rng.randint(0, 50_000_000)
            pct = rng.randint(0, 100)
            s = split_commission(total, pct)

            assert s.professional_pesewas + s.commission_pesewas == total
            assert s.commission_pesewas <= total * pct / 100
            assert s.professional_pesewas >= 0
            assert s.commission_pesewas >= 0

    def test_zero_amount(self):
        s = split_commission(0, 15)
        assert s.professional_pesewas == 0
        assert s.commission_pesewas == 0

    def test_one_pesewa_goes_to_the_professional(self):
        """The smallest possible amount cannot round away to the platform."""
        s = split_commission(1, 15)
        assert s.professional_pesewas == 1
        assert s.commission_pesewas == 0

    def test_full_commission(self):
        s = split_commission(125_000, 100)
        assert s.professional_pesewas == 0
        assert s.commission_pesewas == 125_000


class TestSplitRejectsBadInput:
    def test_rejects_float(self):
        """
        Money is never a float in this codebase. Accepting one here would
        let a rounding error in from anywhere upstream.
        """
        with pytest.raises(TypeError, match='must be int'):
            split_commission(1250.50, 15)

    def test_rejects_boolean_amount(self):
        with pytest.raises(TypeError):
            split_commission(True, 15)

    def test_rejects_decimal_via_type_check(self):
        from decimal import Decimal
        with pytest.raises(TypeError):
            split_commission(Decimal('1250.50'), 15)

    def test_rejects_negative_total(self):
        with pytest.raises(ValueError):
            split_commission(-100, 15)

    @pytest.mark.parametrize('pct', [-1, 101, 1000])
    def test_rejects_impossible_percentages(self, pct):
        with pytest.raises(ValueError):
            split_commission(1000, pct)

    @pytest.mark.parametrize('pct', [True, 15.0, '15'])
    def test_rejects_non_integer_percentages(self, pct):
        with pytest.raises(TypeError):
            split_commission(1000, pct)

    def test_split_refuses_to_construct_if_it_does_not_reconcile(self):
        """
        The dataclass validates itself, so a bad Split cannot exist even if
        something constructs one directly rather than going through
        split_commission.
        """
        with pytest.raises(ValueError, match='does not reconcile'):
            Split(
                total_pesewas=1000,
                professional_pesewas=800,
                commission_pesewas=100,   # 900 != 1000
                commission_percent=15,
            )


class TestFormatGhs:
    @pytest.mark.parametrize('pesewas,expected', [
        (0, 'GHS 0.00'),
        (1, 'GHS 0.01'),
        (50, 'GHS 0.50'),
        (100, 'GHS 1.00'),
        (125_000, 'GHS 1,250.00'),
        (106_250, 'GHS 1,062.50'),
        (18_750, 'GHS 187.50'),
        (100_000_000, 'GHS 1,000,000.00'),
    ])
    def test_formats_for_display(self, pesewas, expected):
        assert format_ghs(pesewas) == expected

    def test_negative_amounts(self):
        """Refunds and reversals are shown as negative, not as an error."""
        assert format_ghs(-125_000) == '-GHS 1,250.00'

    def test_rejects_float(self):
        with pytest.raises(TypeError):
            format_ghs(1250.5)

    def test_rejects_boolean_amount(self):
        with pytest.raises(TypeError):
            format_ghs(True)
