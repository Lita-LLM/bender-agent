"""Охота 26.11: цикл встаёт на 5–8 с на stat файла секретов и чтении настроек при свободном процессоре и iowait 0%.

Замок базы уже справедливый, а виновника по стеку не видно: везде «обычно мгновенный» вызов. Здесь — улики: канарейка
файловой системы (отдельный поток), счётчики ядра до и после остановки, вывод «причина», и меньше обращений цикла к диску."""
import asyncio
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from bender.body import load as cpu
from bender.core import cause, fsprobe
from bender.core import secrets as secrets_module
from bender.core.app import App
from bender.core.cause import GcPauses, explain
from bender.core.fsprobe import FsCanary
from bender.core.loadwatch import alert_text
from bender.core.settings import Settings
from bender.core.stall import LoopWatch, told
from bender.core.store import Store
from tests.helpers import TempDirCase, async_test

PROC = os.path.isdir('/proc/self/task')


class FakeNode(TempDirCase):
    """/proc и /sys в каталоге теста: монтирования и носитель под ними."""

    def setUp(self):
        super().setUp()
        self.proc, self.sys = self.tmp / 'proc', self.tmp / 'sys'
        (self.proc / 'self').mkdir(parents=True)

    def mountinfo(self, *lines):
        (self.proc / 'self' / 'mountinfo').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def disk(self, dev: str, path: str, whole: str, part: str | None = None, rotational: str = '0', model: str = ''):
        """Носитель: /sys/dev/block/<dev> → .../block/<whole>[/<part>]; queue и device лежат у диска."""
        base = self.sys / 'devices' / path / 'block' / whole
        (base / 'queue').mkdir(parents=True, exist_ok=True)
        (base / 'queue' / 'rotational').write_text(rotational + '\n')
        (base / 'device').mkdir(exist_ok=True)
        (base / 'device' / 'model').write_text(model + '\n')
        target = base
        if part:
            target = base / part
            target.mkdir(exist_ok=True)
            (target / 'partition').write_text('1\n')
        (self.sys / 'dev' / 'block').mkdir(parents=True, exist_ok=True)
        os.symlink(target, self.sys / 'dev' / 'block' / dev)

    def where(self, path='/data/bender'):
        return fsprobe.where(path, self.proc, self.sys)


class WhereTest(FakeNode):
    def test_usb_hard_disk_is_named_and_warned_about(self):
        self.mountinfo('1 0 8:1 / / rw - ext4 /dev/sda1 rw', '2 1 8:17 / /data rw,relatime shared:1 - ext4 /dev/sdb1 rw')
        self.disk('8:17', 'pci0000:00/0000:00:14.0/usb1/1-1/1-1:1.0/host2/target2:0:0/2:0:0:0', 'sdb', 'sdb1', rotational='1', model='My Passport')
        info = self.where()
        self.assertIn('ext4 на /dev/sdb1', info['text'])
        self.assertIn('вращающийся USB-диск «My Passport»', info['text'])
        self.assertIn('раскрутки', info['warn'])
        self.assertEqual(info['device'], 'sdb1')

    def test_the_deepest_mount_wins_and_a_prefix_is_not_a_parent(self):
        self.mountinfo('1 0 8:1 / / rw - ext4 /dev/sda1 rw', '2 1 8:17 / /data rw - ext4 /dev/sdb1 rw')
        self.disk('8:1', 'pci0000:00/ata1/host0/target0:0:0/0:0:0:0', 'sda', 'sda1')
        self.disk('8:17', 'pci0000:00/ata2/host1/target1:0:0/1:0:0:0', 'sdb', 'sdb1')
        self.assertIn('/dev/sdb1', self.where('/data/bender')['text'])
        self.assertIn('/dev/sda1', self.where('/database/bender')['text'])  # /database лежит не в /data

    def test_sd_card_gets_its_own_warning(self):
        self.mountinfo('1 0 179:2 / / rw - ext4 /dev/mmcblk0p2 rw')
        self.disk('179:2', 'platform/soc/mmc_host/mmc0/mmc0:0001', 'mmcblk0', 'mmcblk0p2')
        info = self.where()
        self.assertIn('SD-карта или eMMC', info['text'])
        self.assertIn('сборке мусора', info['warn'])

    def test_network_filesystem_is_called_dangerous_for_sqlite(self):
        self.mountinfo('1 0 8:1 / / rw - ext4 /dev/sda1 rw', '2 1 0:55 / /data rw - nfs4 192.168.1.5:/export rw')
        info = self.where()
        self.assertIn('nfs4', info['text'])
        self.assertIn('сетевая файловая система', info['text'])
        self.assertIn('SQLite', info['warn'])
        self.assertEqual(info['device'], '')

    def test_fuse_is_named_but_system_fuse_mounts_are_not_blamed(self):
        self.mountinfo('1 0 0:60 / /data rw - fuse.sshfs user@host:/srv rw', '2 1 0:61 / /run/user/1000/doc rw - fuse.portal portal rw')
        self.assertIn('FUSE', self.where('/data/x')['warn'])
        self.assertEqual(self.where('/run/user/1000/doc/x')['warn'], '')

    def test_virtual_disk_that_calls_itself_rotating_is_not_a_hard_disk(self):
        self.mountinfo('1 0 254:1 / / rw - ext4 /dev/vda1 rw')
        self.disk('254:1', 'pci0000:00/0000:00:04.0/virtio1', 'vda', 'vda1', rotational='1')
        info = self.where()
        self.assertEqual(info['warn'], '')
        self.assertIn('виртуальный диск', info['text'])

    def test_unknown_things_do_not_raise(self):
        self.assertEqual(self.where()['text'], 'неизвестна')  # mountinfo нет
        self.mountinfo('1 0 0:99 / / rw - overlay overlay rw', 'мусор без разделителя')
        info = self.where()
        self.assertIn('слой контейнера', info['text'])
        self.assertEqual(info['device'], '')

    def test_paths_with_spaces_in_mountinfo_are_decoded(self):
        self.mountinfo(r'2 1 8:17 / /mnt/my\040disk rw - ext4 /dev/sdb1 rw')
        self.assertEqual(fsprobe.mount_of('/mnt/my disk/bender', self.proc)['mount'], '/mnt/my disk')


class CanaryTest(TempDirCase):
    def canary(self, **kw):
        folder = self.tmp / 'local'
        folder.mkdir(exist_ok=True)
        return FsCanary(self.tmp / 'b.db', folder, **kw)

    def test_a_slow_probe_is_remembered_with_its_length(self):
        canary = self.canary()
        started = time.time()
        with mock.patch.dict(fsprobe.SLOW, {'stat': 0.05}):
            canary.probe('stat', lambda: time.sleep(0.12))
            canary.probe('stat', lambda: None)
        events = canary.during(started - 1, time.time() + 1)
        self.assertEqual([e['kind'] for e in events], ['stat'])
        self.assertGreaterEqual(events[0]['seconds'], 0.1)
        self.assertFalse(events[0]['ongoing'])
        self.assertEqual(canary.during(started - 100, started - 50), [])

    def test_a_probe_that_has_not_returned_yet_is_the_main_evidence(self):
        canary = self.canary()
        hold, started = threading.Event(), threading.Event()
        with mock.patch.dict(fsprobe.SLOW, {'stat': 0.05}):
            thread = threading.Thread(target=canary.probe, args=('stat', lambda: (started.set(), hold.wait(5))))
            thread.start()
            self.assertTrue(started.wait(2))
            time.sleep(0.12)
            try:
                self.assertEqual(canary.status()['kind'], 'stat')
                hung = canary.during(time.time() - 5, time.time() + 1)
                self.assertEqual([(e['kind'], e['ongoing']) for e in hung], [('stat', True)])
            finally:
                hold.set()
                thread.join(5)
        self.assertIsNone(canary.status())

    def test_probes_work_on_a_real_database_and_do_not_wait_for_the_store_lock(self):
        store = Store(self.tmp / 'b.db')
        self.addCleanup(store.close)
        canary = self.canary()
        done = []

        def run():
            canary.probe('stat', canary._stat)
            canary.probe('read', canary._read)
            canary.probe('fsync', canary._write)
            done.append(True)

        with store._lock:  # замок базы занят нами, канарейке он не нужен
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(5)
        self.assertEqual(done, [True], 'канарейка ждала замок базы Бендера: она ничего не докажет')
        self.assertEqual((self.tmp / 'local' / fsprobe.PROBE_FILE).read_bytes(), b'1')
        self.assertEqual(canary.errors, 0)
        self.assertTrue(all(canary.series[k] for k in ('stat', 'read', 'fsync')))

    def test_missing_files_are_counted_not_raised(self):
        canary = FsCanary(self.tmp / 'nothing' / 'b.db', self.tmp / 'nothing')
        for kind, action in (('stat', canary._stat), ('read', canary._read), ('fsync', canary._write)):
            canary.probe(kind, action)
        self.assertEqual(canary.errors, 3)

    def test_the_directory_next_to_the_database_is_probed_too(self):
        canary = self.canary()
        seen = []
        real = os.stat
        with mock.patch('bender.core.fsprobe.os.stat', lambda path, *a, **k: (seen.append(Path(path)), real(path, *a, **k))[1]):
            canary._stat()
        self.assertEqual(seen, [self.tmp / 'b.db', self.tmp / 'local'])  # базы может ещё не быть — каталог проверяется в любом случае


    def test_the_thread_probes_by_itself_and_can_be_switched_off(self):
        Store(self.tmp / 'b.db').close()
        canary = self.canary(interval=0.01)
        canary.start()
        self.addCleanup(canary.stop)
        deadline = time.time() + 5
        while time.time() < deadline and not (len(canary.series['stat']) >= 3 and canary.series['read']):
            time.sleep(0.02)
        self.assertGreaterEqual(len(canary.series['stat']), 3)
        self.assertTrue(canary.series['read'])
        self.assertTrue(canary.info)  # где лежат данные — узнаёт тот же поток
        canary.enabled = False
        time.sleep(0.05)
        count = len(canary.series['stat'])
        time.sleep(0.15)
        self.assertEqual(len(canary.series['stat']), count, 'выключенная проба продолжает ходить на диск')

    def test_the_line_for_load_says_where_data_lives_and_how_the_disk_behaved(self):
        canary = self.canary()
        self.assertEqual(canary.line(), '')  # пока неизвестно, где лежат данные — молчит
        canary.info = {'text': 'ext4 на /dev/sdb1, вращающийся диск', 'warn': 'вращающийся диск засыпает'}
        now = time.time()
        canary.series['stat'].append((now, 0.0004))
        text = canary.line()
        self.assertIn('Данные: ext4 на /dev/sdb1', text)
        self.assertIn('stat базы 0,4 мс', text)
        self.assertIn('Зависаний не было', text)
        self.assertIn('Внимание: вращающийся диск засыпает', text)
        canary.slow.append({'at': now - 8.1, 'kind': 'stat', 'seconds': 8.1})
        canary.series['stat'].append((now, 8.1))
        text = canary.line()
        self.assertIn('Зависаний файловой системы за час: 1 (дольше всех 8.1 с, stat базы', text)
        canary.enabled = False
        self.assertIn('выключена', canary.line())


def snapshot(mono=0.0, **kw):
    """Снимок счётчиков с нулями; kw меняют нужные."""
    base = {'mono': mono, 'wall': 1000 + mono, 'task': {'state': 'S', 'minflt': 0, 'majflt': 0, 'ticks': 0}, 'sched': {'run': 0.0, 'wait': 0.0},
            'proc_cpu': 0.0, 'psi': {'cpu_some': 0.0, 'io_some': 0.0, 'io_full': 0.0, 'memory_some': 0.0, 'memory_full': 0.0},
            'sys': {'user': 0, 'nice': 0, 'system': 0, 'idle': 0, 'iowait': 0, 'irq': 0, 'softirq': 0, 'steal': 0}, 'ctxt': 0,
            'vm': {'pswpin': 0, 'pswpout': 0, 'pgmajfault': 0}, 'throttle': None, 'disk': None}
    for key, value in kw.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def pair(seconds=8.0, **changes):
    """(до, после): после — то же через seconds секунд, плюс изменения."""
    return snapshot(0.0), snapshot(seconds, **changes)


class ExplainTest(unittest.TestCase):
    def test_a_hung_canary_names_the_filesystem(self):
        before, after = pair(ctxt=9000)
        result = explain(before, after, 7.0, {'canary': [{'kind': 'stat', 'seconds': 6.5, 'ongoing': False}], 'fs': 'nfs4 (192.168.1.5:/export)'})
        self.assertIn('файловая система не отвечала (nfs4 (192.168.1.5:/export))', result['cause'])
        self.assertIn('stat базы из отдельного потока висел 6,5 с', result['cause'])

    def test_a_canary_that_answered_clears_the_disk_and_leaves_the_rest_open(self):
        before, after = pair(ctxt=9000)
        result = explain(before, after, 7.0, {'canary_fast': '0,4 мс', 'fs': 'ext4 на /dev/sda1'})
        self.assertIn('ядро, процессор, память и диск в норме — цикл ждал сам код Бендера', result['cause'])
        self.assertNotIn('файловая система не отвечала', result['cause'])
        text = '\n'.join(result['facts'])
        self.assertIn('Канарейка файловой системы отвечала быстро (самая долгая проба 0,4 мс)', text)
        self.assertIn('Данные лежат на: ext4 на /dev/sda1', text)
        self.assertIn('Ядро:', text)

    def test_waiting_in_the_scheduler_queue_is_a_processor_problem_not_a_disk(self):
        before, after = pair(sched={'wait': 6.0}, throttle=5.0)
        before['throttle'] = 0.0
        result = explain(before, after, 7.0, {})
        self.assertTrue(result['cause'].startswith('процессор:'), result)
        self.assertIn('стоял в очереди за процессором 6,0 с', result['cause'])

    def test_stolen_time_is_blamed_on_the_hypervisor(self):
        before, after = pair(sys={'steal': 4 * cpu.HZ})
        self.assertIn('гипервизор отнял процессор 4,0 с', explain(before, after, 8.0, {})['cause'])

    def test_the_whole_node_standing_still_is_told_apart_from_a_frozen_process(self):
        notes = {'self_late': 7.5}
        before, after = pair(ctxt=3)
        self.assertIn('узел замирал целиком', explain(before, after, 8.0, notes)['cause'])
        before, after = pair(ctxt=80000)
        cause_text = explain(before, after, 8.0, notes)['cause']
        self.assertIn('весь процесс Бендера не выполнялся', cause_text)
        self.assertNotIn('узел замирал', cause_text)

    def test_a_busy_process_that_overslept_is_a_gil_hog_not_a_frozen_one(self):
        before, after = pair(ctxt=80000, proc_cpu=7.5)
        self.assertEqual(explain(before, after, 8.0, {'self_late': 7.0})['cause'], '')

    def test_garbage_collection_is_named(self):
        before, after = pair(ctxt=9000)
        self.assertIn('сборщик мусора Python держал все потоки 6,0 с', explain(before, after, 8.0, {'gc': 6.0})['cause'])

    def test_a_thread_sleeping_in_the_kernel_on_io_is_a_disk_problem(self):
        before, after = pair(ctxt=9000, psi={'io_some': 5.0}, disk=None)
        result = explain(before, after, 8.0, {'stuck': {'state': 'D', 'wchan': 'jbd2_log_wait_commit'}})
        self.assertIn('диск: поток цикла в ядре ждал ввода-вывода (состояние D, jbd2_log_wait_commit)', result['cause'])
        self.assertIn('задачи узла ждали диск 5,0 с', result['cause'])

    def test_page_faults_and_swap_point_at_memory(self):
        before, after = pair(ctxt=9000, task={'majflt': 900}, vm={'pswpin': 5000, 'pswpout': 2000})
        self.assertIn('память: поток ждал страницы', explain(before, after, 8.0, {})['cause'])

    def test_no_more_than_two_reasons_are_told_but_all_are_kept(self):
        before, after = pair(sched={'wait': 6.0}, sys={'steal': 4 * cpu.HZ}, task={'majflt': 900})
        result = explain(before, after, 8.0, {'gc': 6.0, 'canary': [{'kind': 'read', 'seconds': 6.0, 'ongoing': True}]})
        self.assertEqual(result['cause'].count('; '), 1)
        self.assertGreaterEqual(len(result['all']), 4)
        self.assertIn('и ещё не вернулся', result['cause'])

    def test_missing_counters_give_fewer_facts_not_an_error(self):
        self.assertEqual(explain(None, None, 8.0, {})['cause'], '')
        result = explain(None, None, 8.0, {'gc': 7.0, 'stuck': {'state': 'S', 'wchan': 'futex_wait_queue'}})
        self.assertIn('сборщик мусора', result['cause'])
        self.assertIn('спит в futex_wait_queue', '\n'.join(result['facts']))
        before, after = pair()
        after['psi'] = None
        after['vm'] = None
        after['sched'] = None
        explain(before, after, 8.0, {})  # не падает


class GcTest(unittest.TestCase):
    def test_a_long_collection_is_remembered_and_counted_inside_the_window(self):
        gcs = GcPauses()
        before = time.time()
        gcs._callback('start', {})
        time.sleep(0.07)
        gcs._callback('stop', {'generation': 2})
        self.assertEqual(len(gcs.events), 1)
        self.assertGreaterEqual(gcs.during(before - 1, time.time() + 1), 0.06)
        self.assertEqual(gcs.during(before - 100, before - 50), 0.0)
        gcs._callback('start', {})
        gcs._callback('stop', {'generation': 0})  # быстрая — не запоминается
        self.assertEqual(len(gcs.events), 1)

    def test_it_hooks_into_the_interpreter_and_lets_go(self):
        import gc
        gcs = GcPauses()
        gcs.install()
        gcs.install()
        self.assertEqual(gc.callbacks.count(gcs._callback), 1)
        gc.collect()
        gcs.remove()
        gcs.remove()
        self.assertNotIn(gcs._callback, gc.callbacks)


class StallCauseTest(TempDirCase):
    class FakeCanary:
        enabled = True
        info = {'text': 'ext4 на /dev/sdb1, вращающийся диск', 'device': ''}
        series = {'stat': [(0, 0.001)]}

        def __init__(self, hung):
            self.hung = hung

        def during(self, start, end):
            return self.hung

        def worst(self, kind, seconds=3600.0):
            return 0.0004

        def stop(self):
            pass

    @async_test
    async def run_stall(self, canary):
        found = []
        watch = LoopWatch(asyncio.get_running_loop(), found.append, self.tmp / 'stalls.txt', stall=0.3, interval=0.05)
        watch.hang = 0
        watch.canary = canary
        watch.start()
        try:
            await asyncio.sleep(0.4)
            time.sleep(1.0)  # цикл стоит в блокирующем вызове — как на stat зависшей файловой системы
            for _ in range(60):
                if found:
                    break
                await asyncio.sleep(0.05)
        finally:
            watch.stop()
        self.assertTrue(found)
        return found[0]

    def test_the_stall_says_the_filesystem_did_not_answer_when_the_canary_hung_too(self):
        record = self.run_stall(self.FakeCanary([{'kind': 'stat', 'seconds': 0.9, 'ongoing': False}]))
        self.assertIn('файловая система не отвечала', record['cause'])
        self.assertIn('Причина: файловая система не отвечала', told(record))
        text = (self.tmp / 'stalls.txt').read_text(encoding='utf-8')
        self.assertIn('Причина: файловая система не отвечала', text)
        self.assertIn('Канарейка файловой системы (отдельный поток): stat базы — 0,9 с', text)

    def test_when_the_canary_answered_the_stall_is_not_pinned_on_the_disk(self):
        record = self.run_stall(self.FakeCanary([]))
        self.assertNotIn('файловая система не отвечала', record['cause'])
        self.assertIn('отвечала быстро', '\n'.join(record['facts']))
        self.assertNotIn('файловая система не отвечала', told(record))
        if PROC:
            self.assertIn('в норме', record['cause'])  # ядро видно целиком: виновен сам код

    @unittest.skipUnless(PROC, 'нужен /proc/self/task')
    def test_real_kernel_counters_land_in_the_report(self):
        record = self.run_stall(None)
        text = '\n'.join(record['facts'])
        self.assertIn('Ядро: за ', text)
        self.assertIn('Поток цикла в момент, когда сторож заметил остановку: состояние', text)

    def test_a_broken_sensor_does_not_kill_the_watcher(self):
        with mock.patch.object(cause.Sensors, 'snapshot', side_effect=RuntimeError('нет /proc')), mock.patch.object(cause.Sensors, 'stuck', side_effect=OSError):
            record = self.run_stall(None)
        self.assertEqual(record['cause'], '')

    def test_the_message_does_not_repeat_the_culprit_it_already_told(self):
        said = 'цикл стоял в store.py:145 one'
        text = alert_text({'reason': f'цикл вставал 3 раза: {said}', 'culprit': said, 'numbers': {}})
        self.assertEqual(text.count(said), 1)
        self.assertIn('Виновник: другой', alert_text({'reason': 'цикл вставал', 'culprit': 'другой', 'numbers': {}}))


class LoadSwitchTest(TempDirCase):
    @async_test
    async def test_the_setting_switches_the_canary_and_the_report_carries_its_line(self):
        app = App(self.tmp / 'data')
        try:
            canary = FsCanary(app.dirs.db, app.dirs.local)
            canary.info = {'text': 'ext4 на /dev/sda1'}
            watch = app.load_watch
            watch.watch = mock.Mock(thread_cpu=True, canary=canary)
            watch.reload()
            self.assertTrue(canary.enabled)
            app.settings.set('load', {'fs_probe': False})
            watch.reload()
            self.assertFalse(canary.enabled)
            self.assertIn('выключена', watch.filesystem())
            app.settings.set('load', {'fs_probe': True})
            watch.reload()
            self.assertTrue(canary.enabled)
            self.assertIn('Данные: ext4 на /dev/sda1', watch.filesystem())
            watch.watch = None
            self.assertEqual(watch.filesystem(), '')
        finally:
            await app.close()


class LessDiskInTheLoopTest(TempDirCase):
    def test_settings_come_from_memory_while_another_thread_holds_the_database(self):
        store = Store(self.tmp / 'b.db')
        self.addCleanup(store.close)
        settings = Settings(store)
        self.assertFalse(settings.get('voice', 'enabled'))  # прогрев кэша
        taken, release, got = threading.Event(), threading.Event(), []

        def holder():
            with store._lock:
                taken.set()
                release.wait(10)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(taken.wait(2))

        def read():
            started = time.monotonic()
            got.append((settings.get('voice', 'enabled'), time.monotonic() - started))

        for every in (1.0, 0.0):  # свежая сверка и сверка, которой пора, но замок занят: кэш отдаётся и там, и там
            got.clear()
            with mock.patch('bender.core.settings.CHECK_EVERY', every):
                reader = threading.Thread(target=read)
                reader.start()
                reader.join(2)
            self.assertFalse(reader.is_alive(), 'чтение настроек встало в очередь за замком базы')
            self.assertLess(got[0][1], 0.5)
        release.set()
        thread.join(5)

    def test_own_writes_are_seen_at_once_and_other_processes_within_the_interval(self):
        store, other = Store(self.tmp / 'b.db'), Store(self.tmp / 'b.db')
        self.addCleanup(store.close)
        self.addCleanup(other.close)
        mine, theirs = Settings(store), Settings(other)
        self.assertFalse(mine.get('voice', 'enabled'))
        with mock.patch('bender.core.settings.CHECK_EVERY', 1000):
            mine.set('voice', {'enabled': True})
            self.assertTrue(mine.get('voice', 'enabled'), 'своя запись не видна сразу')
            theirs.set('voice', {'enabled': False})
            self.assertTrue(mine.get('voice', 'enabled'), 'чужая запись видна раньше срока сверки: сверка идёт каждое чтение')
        with mock.patch('bender.core.settings.CHECK_EVERY', 0.05):
            time.sleep(0.1)
            self.assertFalse(mine.get('voice', 'enabled'), 'чужая запись не подхватилась по истечении срока сверки')

    def test_a_transaction_of_its_own_thread_never_reads_the_cache(self):
        store = Store(self.tmp / 'b.db')
        self.addCleanup(store.close)
        settings = Settings(store)
        self.assertFalse(settings.get('voice', 'enabled'))
        with store.tx():
            store.x("UPDATE settings SET value=? WHERE grp='voice'", (json.dumps({'enabled': True}),)) if store.one("SELECT 1 FROM settings WHERE grp='voice'") else \
                store.x("INSERT INTO settings(grp,value,updated_at) VALUES('voice',?,?)", (json.dumps({'enabled': True}), time.time()))
            self.assertIsNone(settings._cached('voice'))

    def test_secrets_file_is_not_stat_ed_on_every_read(self):
        path = self.tmp / 'secrets.json'
        keys = secrets_module.Secrets(path)
        keys.set('a', 'один')
        calls = []
        real = Path.stat

        def counting(self, *args, **kwargs):
            if self == path:
                calls.append(1)
            return real(self, *args, **kwargs)

        with mock.patch.object(Path, 'stat', counting):
            for _ in range(50):
                self.assertEqual(keys.get('a'), 'один')
        self.assertEqual(calls, [], 'каждое чтение ключа ходит на диск за stat файла')
        path.write_text(json.dumps({'a': {'value': 'два'}}), encoding='utf-8')
        self.assertEqual(keys.get('a'), 'один')  # правка вручную подхватится по истечении срока
        with mock.patch.object(secrets_module, 'CHECK_EVERY', 0):
            self.assertEqual(keys.get('a'), 'два')

    def test_a_manual_edit_of_secrets_is_not_lost_by_the_next_write(self):
        path = self.tmp / 'secrets.json'
        keys = secrets_module.Secrets(path)
        keys.set('a', 'один')
        path.write_text(json.dumps({'a': {'value': 'один'}, 'b': {'value': 'два-два'}}), encoding='utf-8')  # правка руками
        keys.set('c', 'три-три')  # запись сверяет файл всегда: без этого правка руками потерялась бы
        self.assertEqual(sorted(json.loads(path.read_text(encoding='utf-8'))), ['a', 'b', 'c'])


class StorageWarningTest(TempDirCase):
    @async_test
    async def test_data_on_a_network_filesystem_is_told_once(self):
        app = App(self.tmp / 'data')
        try:
            notes = []
            app.bus.listen(lambda ev: notes.append(ev) if ev.get('type') == 'notify' else None)
            watch = app.load_watch
            watch.loop = asyncio.get_running_loop()
            canary = FsCanary(app.dirs.db, app.dirs.local)
            canary.info = {'text': 'nfs4 (192.168.1.5:/export) — сетевая файловая система', 'warn': 'данные лежат на сетевой файловой системе: SQLite на ней подвисает',
                           'hard': True}
            watch.watch = mock.Mock(thread_cpu=True, canary=canary)
            watch.reload()
            self.assertIn('сетевой файловой системе', watch.check_storage())
            await asyncio.sleep(0.05)
            self.assertEqual(len(notes), 1)
            self.assertIn('nfs4', notes[0]['text'])
            self.assertEqual(watch.check_storage(), '')  # о том же месте — один раз
            canary.info = {'text': 'ext4 на /dev/sda1, SSD', 'warn': '', 'hard': False}
            self.assertEqual(watch.check_storage(), '')
        finally:
            await app.close()

    def test_soft_warnings_stay_in_load_and_do_not_wake_the_owner(self):
        info = fsprobe.where(self.tmp, *self.fake_sd())
        self.assertTrue(info['warn'])
        self.assertFalse(info['hard'])

    def fake_sd(self):
        proc, sys_root = self.tmp / 'proc', self.tmp / 'sys'
        (proc / 'self').mkdir(parents=True)
        (proc / 'self' / 'mountinfo').write_text('1 0 179:2 / / rw - ext4 /dev/mmcblk0p2 rw\n', encoding='utf-8')
        base = sys_root / 'devices' / 'soc' / 'mmc0' / 'block' / 'mmcblk0'
        (base / 'queue').mkdir(parents=True)
        (base / 'queue' / 'rotational').write_text('0\n')
        part = base / 'mmcblk0p2'
        part.mkdir()
        (part / 'partition').write_text('1\n')
        (sys_root / 'dev' / 'block').mkdir(parents=True)
        os.symlink(part, sys_root / 'dev' / 'block' / '179:2')
        return proc, sys_root

    def test_network_and_fuse_are_hard_system_fuse_is_not(self):
        for line, hard in (('1 0 0:55 / / rw - nfs4 host:/x rw', True), ('1 0 0:55 / / rw - cifs //nas/x rw', True), ('1 0 0:60 / / rw - fuse.sshfs u@h:/x rw', True),
                           ('1 0 0:60 / / rw - fuse.portal portal rw', False), ('1 0 0:60 / / rw - ext4 /dev/sda1 rw', False)):
            proc = self.tmp / f'p{abs(hash(line))}'
            (proc / 'self').mkdir(parents=True)
            (proc / 'self' / 'mountinfo').write_text(line + '\n', encoding='utf-8')
            self.assertEqual(fsprobe.where('/x', proc, self.tmp / 'nosys')['hard'], hard, line)


if __name__ == '__main__':
    unittest.main()
