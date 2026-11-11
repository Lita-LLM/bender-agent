"""Публичный профиль: один конкретный просмотр хозяином, строгий JPG и обычный Bot API."""
import json
from unittest.mock import patch
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv
from tests import test_telegram as original

JPG=b'\xff\xd8\xff\xe0'+b'fake-jpg'


class ProfileTest(TempDirCase):
    _up=original.TelegramTest._up
    until=original.TelegramTest.until
    sent=original.TelegramTest.sent

    @async_test
    async def test_agent_needs_one_approval_even_guard_off_and_updates_languages(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app
            app.selfdir.set_identity({'name':'Тин','creature':'Робот','vibe':'Спокойный помощник'})
            app.settings.set('guard',{'enabled':False})
            env.auto_approve('once')
            result=await app.tools.run('telegram_profile',{},ToolContext(app,session_id=env.sid))
            self.assertTrue(result.ok,result.content)
            self.assertEqual(len(env.approvals),1)
            self.assertEqual(env.approvals[0]['capability'],'telegram.profile')
            for method in ('setMyName','setMyDescription','setMyShortDescription'):
                calls=self.sent(env,method)
                self.assertEqual({c['language_code'] for c in calls},{'','ru'})
            self.assertEqual(self.sent(env,'setMyName')[0]['name'],'Тин')
            self.assertEqual(app.store.meta_get('telegram:profile')['values']['short_description'],'Спокойный помощник')

    @async_test
    async def test_denied_unattended_or_other_agent_do_not_change_public_bot(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app
            env.auto_approve('deny')
            result=await app.tools.run('telegram_profile',{'name':'Не применять'},ToolContext(app,session_id=env.sid))
            self.assertFalse(result.ok)
            self.assertEqual(self.sent(env,'setMyName'),[])
            result=await app.tools.run('telegram_profile',{'name':'Ночью'},ToolContext(app,session_id=env.sid,unattended=True))
            self.assertFalse(result.ok)
            self.assertEqual(len(env.approvals),1)
            child=app.agents.get(app.agents.create('Сосед',awaken=False)['id'])
            result=await child.tools.run('telegram_profile',{},ToolContext(child,session_id=child.main_session()))
            self.assertFalse(result.ok)
            self.assertEqual(self.sent(env,'setMyName'),[])

    @async_test
    async def test_jpg_own_work_only_no_svg_conversion_and_owner_caption(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app;tg=app.telegram
            jpg=app.dirs.work/'avatar.jpg';jpg.write_bytes(JPG)
            svg=app.dirs.work/'avatar.svg';svg.write_text('<svg/>')
            env.auto_approve('once')
            rejected=await app.tools.run('telegram_profile',{'avatar':str(svg)},ToolContext(app,session_id=env.sid))
            self.assertFalse(rejected.ok);self.assertEqual(env.approvals,[])
            outside=self.tmp/'outside.jpg';outside.write_bytes(JPG)
            rejected=await app.tools.run('telegram_profile',{'avatar':str(outside)},ToolContext(app,session_id=env.sid))
            self.assertFalse(rejected.ok)
            applied=await app.tools.run('telegram_profile',{'avatar':str(jpg)},ToolContext(app,session_id=env.sid))
            self.assertTrue(applied.ok,applied.content)
            upload=self.sent(env,'setMyProfilePhoto')[-1]
            self.assertEqual(json.loads(upload['photo']),{'type':'static','photo':'attach://avatar'})
            self.assertEqual(upload['files']['avatar']['bytes'],len(JPG))
            self.assertEqual(self.sent(env,'setMyName'),[])
            before=len(env.approvals)
            env.fake.state.tg_files['photo']={'file_path':'files/avatar.jpg','raw':JPG}
            await tg._handle({'message':{'message_id':40,'from':{'id':42},'chat':{'id':42,'type':'private'},'caption':'/avatar','photo':[{'file_id':'photo'}]}})
            self.assertEqual(len(self.sent(env,'setMyProfilePhoto')),2)
            self.assertEqual(len(env.approvals),before)
            self.assertEqual([r for r in env.fake.state.requests if r.get('api')=='openai'],[])

    @async_test
    async def test_command_profile_is_preview_until_owner_clicks(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app;tg=app.telegram
            app.selfdir.set_identity({'name':'Хозяин подтвердит'})
            await tg.commands.dispatch(app,42,env.sid,'profile')
            self.assertEqual(self.sent(env,'setMyName'),[])
            button=self.sent(env)[-1]['reply_markup']['inline_keyboard'][0][0]
            await tg._callback({'id':'profile','from':{'id':42},'data':button['callback_data'],'message':{'chat':{'id':42},'message_id':1}})
            self.assertEqual(self.sent(env,'setMyName')[0]['name'],'Хозяин подтвердит')
