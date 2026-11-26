"""Справедливый замок базы: очередь, рекурсия, срок ожидания, и главное — рабочий поток не голодит остальных."""
import threading
import time
import unittest

from bender.core.fairlock import FairRLock


def started(thread):
    thread.start()
    return thread


class FairLockTests(unittest.TestCase):
    def test_recursion_and_release_by_stranger(self):
        lock = FairRLock()
        with lock:
            with lock:
                self.assertTrue(lock._is_owned())
            self.assertTrue(lock._is_owned())
        self.assertFalse(lock._is_owned())
        self.assertEqual(lock.holder()[0], None)
        with self.assertRaises(RuntimeError):
            lock.release()
        with lock:
            errors = []
            started(threading.Thread(target=lambda: errors.append(self._release(lock)))).join()
            self.assertEqual(errors, ['RuntimeError'])

    @staticmethod
    def _release(lock):
        try:
            lock.release()
        except RuntimeError:
            return 'RuntimeError'
        return 'released'

    def test_exception_inside_with_frees_the_lock(self):
        lock = FairRLock()
        with self.assertRaises(ValueError):
            with lock:
                raise ValueError('x')
        self.assertTrue(lock.acquire(False))
        lock.release()

    def test_waiters_get_it_in_order_of_arrival(self):
        lock, order, ready = FairRLock(), [], []
        lock.acquire()

        def wait(name):
            ready.append(name)
            with lock:
                order.append(name)

        threads = []
        for name in ('a', 'b', 'c', 'd'):
            threads.append(started(threading.Thread(target=wait, args=(name,))))
            deadline = time.monotonic() + 5
            while lock.waiting() < len(threads) and time.monotonic() < deadline:  # встал в очередь — следующий
                time.sleep(0.001)
        self.assertEqual(lock.waiting(), 4)
        lock.release()
        for thread in threads:
            thread.join(5)
        self.assertEqual(order, ['a', 'b', 'c', 'd'])
        self.assertEqual(lock.waiting(), 0)

    def test_releaser_does_not_retake_before_waiter(self):
        """Ровно то, чего не делает RLock: отпустил и тут же взял снова — ждущий всё равно проходит первым."""
        lock, log = FairRLock(), []
        lock.acquire()
        waiter = started(threading.Thread(target=lambda: (lock.acquire(), log.append('waiter'), lock.release())))
        deadline = time.monotonic() + 5
        while not lock.waiting() and time.monotonic() < deadline:
            time.sleep(0.001)
        lock.release()
        lock.acquire()  # тот же поток берёт сразу после release: очередь впереди
        log.append('releaser')
        lock.release()
        waiter.join(5)
        self.assertEqual(log, ['waiter', 'releaser'])

    def test_worker_taking_it_batch_after_batch_cannot_starve_the_loop(self):
        lock, stop = FairRLock(), threading.Event()
        limit = time.monotonic() + 6  # при возврате к RLock тест падает по сроку, а не висит

        def batches():
            while not stop.is_set() and time.monotonic() < limit:
                with lock:
                    time.sleep(0.05)  # пачка работы под замком, между пачками — ни мгновения паузы

        worker = started(threading.Thread(target=batches, daemon=True))
        time.sleep(0.1)
        waits = []
        try:
            for _ in range(8):
                begin = time.monotonic()
                with lock:
                    waits.append(time.monotonic() - begin)
                time.sleep(0.01)
        finally:
            stop.set()
            worker.join(5)
        # с RLock здесь выходили секунды (замер 3.11–3.13); с очередью — не дольше пачки и накладных расходов
        self.assertLess(max(waits), 0.4, waits)

    def test_non_blocking_and_timeout(self):
        lock = FairRLock()
        lock.acquire()
        results = []
        started(threading.Thread(target=lambda: results.append(lock.acquire(False)))).join()
        begin = time.monotonic()
        started(threading.Thread(target=lambda: results.append(lock.acquire(True, 0.1)))).join()
        self.assertEqual(results, [False, False])
        self.assertGreaterEqual(time.monotonic() - begin, 0.09)
        self.assertEqual(lock.waiting(), 0)  # не дождавшийся из очереди ушёл
        lock.release()
        self.assertTrue(lock.acquire(False))
        lock.release()

    def test_timeout_that_races_with_handoff_never_loses_the_lock(self):
        for _ in range(200):
            lock, got = FairRLock(), []
            lock.acquire()
            waiter = started(threading.Thread(target=lambda: got.append(lock.acquire(True, 0.002))))
            time.sleep(0.0015)
            lock.release()
            waiter.join(5)
            if got[0]:
                # замок отдали — он обязан быть у ждавшего, а не потеряться
                self.assertIsNotNone(lock.holder()[0])
            else:
                self.assertEqual(lock.holder()[0], None)
            self.assertEqual(lock.waiting(), 0)

    def test_holder_and_waiting_for_diagnostics(self):
        lock = FairRLock()
        self.assertEqual(lock.holder(), (None, 0.0))
        with lock:
            ident, held = lock.holder()
            self.assertEqual(ident, threading.get_ident())
            time.sleep(0.02)
            self.assertGreaterEqual(lock.holder()[1], 0.015)
            waiter = started(threading.Thread(target=lambda: (lock.acquire(), lock.release())))
            deadline = time.monotonic() + 5
            while lock.waiting() < 1 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(lock.waiting(), 1)
        waiter.join(5)
        self.assertEqual(lock.waiting(), 0)

    def test_many_threads_nested_counter_is_exact(self):
        lock, total = FairRLock(), [0]

        def work():
            for _ in range(300):
                with lock:
                    with lock:
                        value = total[0]
                        total[0] = value + 1

        threads = [started(threading.Thread(target=work)) for _ in range(8)]
        for thread in threads:
            thread.join(30)
        self.assertEqual(total[0], 8 * 300)
        self.assertEqual(lock.holder()[0], None)


if __name__ == '__main__':
    unittest.main()
