"""Unit tests for robotsix_mill.runtime.credit_status."""

import threading

from robotsix_mill.runtime.credit_status import (
    clear_credit_status,
    get_credit_status,
    record_balance_low,
    record_balance_ok,
    record_low_credit,
)


class TestRecordLowCredit:
    def test_sets_low_true_and_timestamp(self):
        record_low_credit(detail="low credits!")
        status = get_credit_status()
        assert status["low"] is True
        assert status["last_402_at"] is not None
        assert status["detail"] == "low credits!"

    def test_preserves_existing_balance(self):
        record_balance_ok(balance_usd=12.34, threshold_usd=5.0)
        record_low_credit(detail="boom")
        status = get_credit_status()
        assert status["low"] is True
        assert status["balance_usd"] == 12.34
        assert status["threshold_usd"] == 5.0

    def test_overwrites_balance_when_passed(self):
        record_balance_ok(balance_usd=12.34, threshold_usd=5.0)
        record_low_credit(balance_usd=1.23, threshold_usd=5.0, detail="boom")
        status = get_credit_status()
        assert status["balance_usd"] == 1.23
        assert status["threshold_usd"] == 5.0


class TestRecordBalanceOk:
    def test_clears_warning(self):
        record_balance_low(balance_usd=2.0, threshold_usd=5.0)
        record_balance_ok(balance_usd=10.0, threshold_usd=5.0)
        status = get_credit_status()
        assert status["low"] is False
        assert status["balance_usd"] == 10.0
        assert status["threshold_usd"] == 5.0

    def test_recovery_after_402(self):
        record_low_credit(detail="402!")
        record_balance_ok(balance_usd=10.0, threshold_usd=5.0)
        status = get_credit_status()
        assert status["low"] is False


class TestRecordBalanceLow:
    def test_sets_low_with_balance(self):
        record_balance_low(balance_usd=2.0, threshold_usd=5.0, detail="low")
        status = get_credit_status()
        assert status["low"] is True
        assert status["balance_usd"] == 2.0
        assert status["threshold_usd"] == 5.0


class TestClearCreditStatus:
    def test_clears_to_healthy(self):
        record_low_credit(detail="x")
        clear_credit_status()
        status = get_credit_status()
        assert status["low"] is False
        assert status["last_402_at"] is None


class TestGetCreditStatus:
    def test_returns_copy_not_reference(self):
        record_balance_ok(balance_usd=10.0, threshold_usd=5.0)
        s1 = get_credit_status()
        s2 = get_credit_status()
        assert s1 == s2
        assert s1 is not s2

    def test_defaults_when_never_written(self):
        clear_credit_status()
        # Clear resets but also sets defaults; test fresh module state
        # by reading after clear.
        s = get_credit_status()
        assert s["low"] is False


class TestConcurrentAccess:
    def test_concurrent_writes(self):
        errors = []

        def write_low():
            try:
                for _ in range(100):
                    record_low_credit(detail="t")
            except Exception as e:
                errors.append(e)

        def write_ok():
            try:
                for _ in range(100):
                    record_balance_ok(balance_usd=10.0, threshold_usd=5.0)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=write_low),
            threading.Thread(target=write_ok),
            threading.Thread(target=write_low),
            threading.Thread(target=write_ok),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors

    def test_concurrent_reads_during_writes(self):
        errors = []
        done = threading.Event()

        def writer():
            while not done.is_set():
                record_low_credit(detail="t")
                record_balance_ok(balance_usd=10.0, threshold_usd=5.0)

        def reader():
            try:
                while not done.is_set():
                    get_credit_status()
            except Exception as e:
                errors.append(e)

        w = threading.Thread(target=writer)
        readers = [threading.Thread(target=reader) for _ in range(4)]
        w.start()
        for r in readers:
            r.start()
        # Let them run briefly
        import time

        time.sleep(0.1)
        done.set()
        w.join()
        for r in readers:
            r.join()
        assert not errors
