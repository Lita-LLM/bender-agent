"""RFC 8032, точный пакет, совместимость данных и настоящий Сторож со слотами."""
import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
import zipfile
from pathlib import Path

from bender.core.app import App
from bender.core.errors import HumanError
from bender.core.paths import PROGRAM_DIR
from bender.core.store import Store
from bender.core.watchdog import load_boot
from bender.updates import ed25519
from bender.updates.manager import META, canonical, manifest, package, snapshot
from tests.helpers import TempDirCase, async_test
from tests.test_watchdog import _api, _free_port, _wait_state, _API_DATA

VECTORS = [
 ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "", "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
 ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72", "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")]


class Ed25519Test(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for seed,public,message,signature in VECTORS:
            seed,public,message,signature=map(bytes.fromhex,(seed,public,message,signature))
            self.assertEqual(ed25519.public_key(seed), public)
            self.assertEqual(ed25519.sign(seed,message),signature)
            self.assertTrue(ed25519.verify(public,message,signature))
            self.assertFalse(ed25519.verify(public,message+b"changed",signature))
            self.assertFalse(ed25519.verify(public,message,signature[:-1]))
            malleable=signature[:32]+(int.from_bytes(signature[32:],"little")+ed25519.L).to_bytes(32,"little")
            self.assertFalse(ed25519.verify(public,message,malleable))
        self.assertFalse(ed25519.verify(b"\x01"+b"\x00"*31,b"",b"\x01"+b"\x00"*63))
        self.assertFalse(ed25519.verify((2**255-1).to_bytes(32,"little"),b"",b"\x00"*64))


class VersionsTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.data=self.tmp/"data"
        self.app=App(self.data)
        self.source=self.tmp/"source"
        snapshot(PROGRAM_DIR,self.source)
        self.app.settings.set("updates",{"allow_development":True,"health_seconds":2})

    def tearDown(self):
        self.app.store.close()
        super().tearDown()

    def test_signed_package_keys_tamper_and_unsigned_archive(self):
        self.app.settings.set("updates",{"allow_development":False})
        seed=bytes.fromhex(VECTORS[0][0])
        public=ed25519.public_key(seed).hex()
        data={"format":1,"version":"v-test","files":manifest(package(self.source))}
        signed={**data,"public_key":public,"signature":ed25519.sign(seed,canonical(data)).hex()}
        (self.source/META).write_text(json.dumps(signed))
        with self.assertRaises(HumanError):self.app.versions.stage(self.source)
        self.app.settings.set("updates",{"public_keys":[public]})
        record=self.app.versions.stage(self.source)
        self.assertTrue(record["signed"])
        self.app.settings.set("updates",{"public_keys":[]})
        with self.assertRaises(HumanError):self.app.versions.install(record["id"],record["hash"])
        self.app.settings.set("updates",{"public_keys":[public]})
        archive=self.tmp/"signed.zip"
        with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as zip:
            for p in self.source.rglob("*"):
                if p.is_file():zip.write(p,p.relative_to(self.source))
        self.assertTrue(self.app.versions.stage(archive)["signed"])
        (self.source/"bender/__init__.py").write_text("# Подмена")
        with self.assertRaises(HumanError):self.app.versions.stage(self.source)
        (self.source/META).unlink()
        self.app.settings.set("updates",{"allow_development":True})
        archive=self.tmp/"unsigned.zip"
        with zipfile.ZipFile(archive,"w") as zip:zip.writestr("../outside.txt","нет")
        with self.assertRaises(HumanError):self.app.versions.stage(archive,development=True)

    @async_test
    async def test_exact_review_protected_slots_and_agent_cannot_change_trust(self):
        from bender.tools.registry import ToolContext
        record=self.app.versions.stage(self.source,development=True)
        with self.assertRaises(HumanError):self.app.versions.install(record["id"],"old")
        staged=self.app.versions.staging/record["id"]
        (staged/"bender/__init__.py").write_text("# Подмена")
        with self.assertRaises(HumanError):self.app.versions.install(record["id"],record["hash"])
        with self.assertRaises(HumanError):self.app.settings.set("updates",{"allow_development":True},by="agent")
        result=await self.app.tools.run("settings",{"group":"updates","changes":{"public_keys":["00"*32]}},ToolContext(self.app))
        self.assertFalse(result.ok)
        for folder in ("versions","workshop","exams"):
            result=await self.app.tools.run("write_file",{"path":str(self.data/folder/"change.py"),"content":"нет"},ToolContext(self.app))
            self.assertFalse(result.ok,folder)
        await self.app.close()

    def test_data_addition_backup_and_old_app_preserves_future(self):
        self.app.store.migrate("future",1,["CREATE TABLE future_data (id INTEGER PRIMARY KEY, value TEXT)","ALTER TABLE sessions ADD COLUMN future_value TEXT"])
        self.app.store.x("INSERT INTO future_data VALUES(1,'сохранить')")
        self.assertTrue(list((self.data/"local/migration-backups").glob("*.db")))
        with self.assertRaises(HumanError):self.app.store.migrate("future",2,["DROP TABLE future_data"])
        self.app.store.close()
        self.app=App(self.data)
        self.assertEqual(self.app.store.one("SELECT value FROM future_data")[0],"сохранить")
        sid=self.app.store.new_session("chat","Старый интерфейс")
        self.app.store.add_message(sid,{"role":"user","content":"Работает"})
        self.assertEqual(self.app.store.messages(sid)[0]["content"],"Работает")

    def start(self):
        self.port=_free_port()
        _API_DATA[self.port]=self.data
        env={k:v for k,v in os.environ.items() if k not in ("BENDER_CORE","BENDER_WATCHED","BENDER_TRIAL","BENDER_SAFE","BENDER_RESTARTED")}
        return subprocess.Popen([sys.executable,"-m","bender","--data",str(self.data),"run","--no-browser","--port",str(self.port)],cwd=PROGRAM_DIR,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)

    def stop(self,p):
        p.send_signal(signal.SIGTERM)
        out,_=p.communicate(timeout=15)
        self.assertEqual(p.returncode,0,out.decode("utf-8","replace")[-3000:])

    def activate(self):
        record=self.app.versions.stage(self.source,development=True)
        self.app.versions.install(record["id"],record["hash"])
        return record

    @unittest.skipUnless(sys.platform.startswith("linux"),"сигналы слота — Linux")
    def test_real_slot_health_and_manual_rollback(self):
        record=self.activate()
        p=self.start()
        try:
            self.assertIsNotNone(_wait_state(self.port))
            for _ in range(80):
                if self.app.versions.public()["health"]:break
                time.sleep(.05)
            current=self.app.versions.public()
            self.assertIsNone(current["pending"],p.poll())
            self.assertEqual(current["health"]["slot"],record["id"])
            self.app.versions.rollback()
            _api(self.port,"/api/restart",{"mode":"same"})
            state=_wait_state(self.port,lambda s:s["boot"]["starts"]>=2)
            self.assertIsNotNone(state)
            self.assertNotEqual(self.app.versions.public()["current"],record["id"])
        finally:self.stop(p)

    @unittest.skipUnless(sys.platform.startswith("linux"),"сигналы слота — Linux")
    def test_two_healthy_updates_in_one_watchdog(self):
        first = self.activate()
        p = self.start()
        try:
            self.assertIsNotNone(_wait_state(self.port))
            for _ in range(100):
                if self.app.versions.public()["health"]:
                    break
                time.sleep(.05)
            self.assertEqual(self.app.versions.public()["health"]["slot"], first["id"])
            source = self.source / "bender/__init__.py"
            source.write_text(source.read_text() + "\n# Второе здоровое обновление\n")
            second = self.activate()
            _api(self.port, "/api/restart", {"mode": "same"})
            for _ in range(160):
                state = self.app.versions.public()
                if (state.get("health") or {}).get("slot") == second["id"]:
                    break
                time.sleep(.05)
            self.assertEqual(state["current"], second["id"])
            self.assertEqual(state["health"]["slot"], second["id"])
            self.assertIsNone(state["last_rollback"])
        finally:
            self.stop(p)

    @unittest.skipUnless(sys.platform.startswith("linux"),"сигналы слота — Linux")
    def test_broken_slot_and_no_health_deadline_rollback_under_minute(self):
        for code in ("raise RuntimeError('Битая версия')", "import time; time.sleep(90)"):
            init=self.source/"bender/__init__.py"
            init.write_text(code)
            self.activate()
            start=time.monotonic()
            p=self.start()
            try:
                state=_wait_state(self.port,tries=150)
                self.assertIsNotNone(state,"Прежняя версия не поднялась")
                boot=load_boot(self.data)
                self.assertTrue(boot.get("rollback"))
                self.assertLess(time.monotonic()-start,60)
                self.assertIsNone(self.app.versions.public()["pending"])
                self.assertFalse(state["safe"])
            finally:self.stop(p)
            # Прежний здоровый слот остаётся основой следующей попытки.
