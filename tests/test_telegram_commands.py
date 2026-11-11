"""Меню по сохранённой справке и команды хозяина без запросов к модели."""
import asyncio
from unittest.mock import patch,AsyncMock
from bender.channels.telegram import OWNER_META,TOKEN_SECRET
from bender.channels.telegram_commands import COMMANDS,SCOPES,menu
from bender.channels.telegram_languages import LANGUAGES
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv
from tests import test_telegram as original


class CommandsTest(TempDirCase):
    _up=original.TelegramTest._up
    until=original.TelegramTest.until
    sent=original.TelegramTest.sent

    @async_test
    async def test_all_languages_clear_scopes_then_set_and_restart_skips_api(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;tg=app.telegram;st=env.fake.state
            old=[{'command':'old','description':'Старый OpenClaw'}]
            st.tg_commands[('all_private_chats','de')]=old
            st.tg_commands[('all_chat_administrators','fr')]=old
            app.settings.set('telegram',{'api_base':env.base+'/tg','enabled':True})
            app.secrets.set(TOKEN_SECRET,st.tg_token)
            app.store.meta_set(OWNER_META,{'user_id':42,'chat_id':42})
            tg.start()
            await self.until(lambda:app.store.meta_get('telegram:commands_hash'),timeout=20)
            calls=st.tg_command_calls
            deletes=[c for c in calls if c['method']=='deleteMyCommands']
            self.assertEqual({(c['scope']['type'],c['language_code']) for c in deletes},{(scope,language) for scope in SCOPES for language in ('','ru','en','de','fr')})
            first_set=next(i for i,c in enumerate(calls) if c['method']=='setMyCommands')
            self.assertTrue(all(c['method']=='setMyCommands' for c in calls[first_set:]))
            self.assertTrue(any(c['method']=='getMyCommands' and c['language_code']=='zu' for c in calls))
            self.assertEqual(len(LANGUAGES),184)
            for language in ('','ru'):
                private=st.tg_commands[('default',language)]
                group=st.tg_commands[('all_group_chats',language)]
                self.assertGreaterEqual(len(private),25)
                self.assertLessEqual(len(private),100)
                self.assertIn('compact',{row['command'] for row in private})
                self.assertLess(len(group),len(private))
                self.assertTrue(all(any('А'<=ch<='я' for ch in row['description']) for row in private))
            count=len(calls)
            await tg.restart()
            await self.until(lambda:tg.state=='ok' and tg.commands.task is not None and tg.commands.task.done())
            self.assertEqual(len(calls),count)
            self.assertTrue(any('Команды бота обновлены' in row['summary'] for row in app.journal.recent(30)))
            before=app.store.meta_get('telegram:commands_hash')
            st.tg_token='456:NEW';app.secrets.set(TOKEN_SECRET,st.tg_token)
            app.settings.set('telegram',{'command_languages':['ru','en']})
            await tg.restart()
            await self.until(lambda:app.store.meta_get('telegram:commands_hash')!=before,timeout=10)
            self.assertGreater(len(calls),count)

    @async_test
    async def test_all_builtin_commands_respond_without_model(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True)
            app=env.app;tg=app.telegram
            with patch.object(app.updater,'check',AsyncMock(return_value={'available':False})),patch.object(app.backups,'now',AsyncMock(return_value={'id':'fake'})):
                for command,_ in COMMANDS:
                    arg={'remember':'Проверочный мандарин в дневнике','recall':'мандарин','live':'text','voice':'off','agent':'main','new':'Другой чат'}.get(command,'')
                    before=len(self.sent(env))
                    self.assertTrue(await tg.commands.dispatch(app,42,env.sid,command,arg),command)
                    self.assertGreater(len(self.sent(env)),before,command)
            self.assertNotIn('Внутренняя ошибка','\n'.join(m['text'] for m in self.sent(env)))
            self.assertNotIn('Неизвестный сбой','\n'.join(m['text'] for m in self.sent(env)))
            self.assertEqual([r for r in env.fake.state.requests if r.get('api')=='openai'],[])
            self.assertIn('мандарин',app.selfdir.read('MEMORY.md'))
            await tg.commands.dispatch(app,42,env.sid,'settings','guard')
            self.assertIn('Предохранитель',self.sent(env)[-1]['text'])
            await tg.commands.dispatch(app,42,env.sid,'settings','not-existing')
            self.assertIn('Неизвестная группа',self.sent(env)[-1]['text'])

    @async_test
    async def test_agent_selection_and_new_chat_route_model_and_reaction_privately(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app;tg=app.telegram
            record=app.agents.create('Сосед',awaken=False);child=app.agents.get(record['id'])
            await tg.commands.dispatch(app,42,env.sid,'agent',child.agent_id)
            self.assertIs(tg.selected(42)[0],child)
            env.script([{'text':'Ответ соседа.'}])
            await tg._handle({'message':{'message_id':8001,'from':{'id':42},'chat':{'id':42,'type':'private'},'text':'Только соседу'}})
            await self.until(lambda:any(m['text']=='Ответ соседа.' for m in self.sent(env)))
            await self.until(lambda:not tg._bg)
            self.assertFalse(app.store.messages(env.sid))
            self.assertEqual(child.store.messages(child.main_session())[0]['content'],'Только соседу')
            first=next(m for m in self.sent(env) if m['text']=='Ответ соседа.')
            await tg._reaction({'chat':{'id':42},'user':{'id':42},'message_id':first['message_id'],'new_reaction':[{'type':'emoji','emoji':'👍'}]})
            self.assertTrue(child.store.meta_get('telegram:reactions:'+child.main_session()))
            self.assertIsNone(app.store.meta_get('telegram:reactions:'+child.main_session()))
            await tg.commands.dispatch(child,42,child.main_session(),'new','Свежий разговор')
            _,sid=tg.selected(42)
            self.assertNotEqual(sid,child.main_session())
            env.script([{'text':'Ответ в новом чате.'}])
            await tg._handle({'message':{'message_id':8002,'from':{'id':42},'chat':{'id':42,'type':'private'},'text':'Новая страница соседа'}})
            await self.until(lambda:any(m['text']=='Ответ в новом чате.' for m in self.sent(env)))
            self.assertEqual(child.store.messages(sid)[0]['content'],'Новая страница соседа')
            self.assertFalse(app.store.messages(env.sid))

    @async_test
    async def test_model_button_owner_only_and_reusable(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app;tg=app.telegram
            pid=app.models.roles()['main'][0]['provider']
            app.models.update_provider(pid,{'models':[{'id':'echo'}]})
            await tg.commands.dispatch(app,42,env.sid,'model')
            buttons=self.sent(env)[-1]['reply_markup']['inline_keyboard']
            callback=next(row[0]['callback_data'] for row in buttons if 'echo' in row[0]['text'])
            def query(uid):return {'id':'choice','data':callback,'from':{'id':uid},'message':{'chat':{'id':42},'message_id':1}}
            await tg._callback(query(99))
            self.assertEqual(app.models.roles()['main'][0]['model'],'script')
            await tg._callback(query(42))
            self.assertEqual(app.models.roles()['main'][0]['model'],'echo')
            app.models.set_role('main',[{'provider':pid,'model':'script'}])
            await tg._callback(query(42))  # срока у выбора нет: та же кнопка срабатывает снова
            self.assertEqual(app.models.roles()['main'][0]['model'],'echo')
            self.assertEqual([r for r in env.fake.state.requests if r.get('api')=='openai'],[])

    @async_test
    async def test_model_choice_goes_through_providers_and_survives_restart(self):
        from bender.channels.telegram_commands import Commands
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app;tg=app.telegram
            other=app.models.add_provider(name='Второй',base_url='http://127.0.0.1:9/v1',extra={'is_local':True,'models':[{'id':'big'},{'id':'small'}]})
            other=other['id'] if isinstance(other,dict) else other
            await tg.commands.dispatch(app,42,env.sid,'models')  # прежнее имя ведёт в /model
            first=self.sent(env)[-1]
            self.assertIn('Модели по ролям',first['text']);self.assertIn('выбери провайдера',first['text'])
            rows=[row[0] for row in first['reply_markup']['inline_keyboard']]
            self.assertTrue(rows[0]['text'].startswith('✓ '))
            second=next(row for row in rows if row['text'].startswith('Второй'))
            self.assertTrue(second['text'].endswith('· 2'))
            def click(button):return tg._callback({'id':'c','data':button['callback_data'],'from':{'id':42},'message':{'chat':{'id':42},'message_id':first['message_id']}})
            await click(second)
            listing=self.sent(env)[-1];models=[row[0] for row in listing['reply_markup']['inline_keyboard']]
            self.assertEqual([b['text'] for b in models],['big','small','◀ Другие провайдеры'])
            tg._commands=Commands(tg)  # перезапуск: кнопки под прежними сообщениями работают
            await click(models[1])
            main=app.models.roles()['main'][0];self.assertEqual((main['provider'],main['model']),(other,'small'))
            await click(models[2])
            self.assertIn('выбери провайдера',self.sent(env)[-1]['text'])

    @async_test
    async def test_dangerous_choices_work_once(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app;tg=app.telegram
            await tg.commands.pick(app,42,env.sid,'control','Выключить?',[('Отмена',{'cancel':True})])
            button=self.sent(env)[-1]['reply_markup']['inline_keyboard'][0][0]
            query={'id':'c','data':button['callback_data'],'from':{'id':42},'message':{'chat':{'id':42},'message_id':1}}
            await tg._callback(query);self.assertEqual(self.sent(env)[-1]['text'],'Отменено.')
            await tg._callback(query);self.assertIn('уже сделан',self.sent(env)[-1]['text'])

    def test_menu_order_puts_prefix_first_and_old_names_are_aliases(self):
        from bender.channels.telegram_commands import ALIASES
        names=[name for name,_ in COMMANDS]
        for i,name in enumerate(names):
            for later in names[:i]:
                self.assertFalse(name!=later and later.startswith(name),f'/{name} должна идти раньше /{later}')
        self.assertEqual(names[:4],['new','chats','agent','model']);self.assertEqual(names[-1],'help')
        self.assertNotIn('models',names);self.assertNotIn('agents',names)
        self.assertEqual((ALIASES['models'],ALIASES['agents']),('model','agent'))

    @async_test
    async def test_model_list_pages_and_search(self):
        """Длинный список моделей в Telegram листается по страницам."""
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app;tg=app.telegram
            pid=app.models.roles()['main'][0]['provider']
            app.models.update_provider(pid,{'models':[{'id':f'model-{n:03d}'} for n in range(150)]})
            await tg.commands.dispatch(app,42,env.sid,'model')
            first=self.sent(env)[-1]
            rows=first['reply_markup']['inline_keyboard']
            self.assertEqual(len(rows),tg.commands.PAGE+1)
            self.assertIn('151 шт.',first['text'])
            nav=rows[-1];self.assertEqual(nav[1]['text'],'1 / 19')
            await tg._callback({'id':'nav','data':nav[2]['callback_data'],'from':{'id':42},'message':{'chat':{'id':42},'message_id':first['message_id']}})
            edit=self.sent(env,'editMessageReplyMarkup')[-1]
            self.assertEqual(edit['message_id'],first['message_id'])
            page=edit['reply_markup']['inline_keyboard']
            self.assertEqual(page[-1][1]['text'],'2 / 19')
            target=next(row[0] for row in page[:-1] if 'model-010' in row[0]['text'])
            await tg._callback({'id':'pick','data':target['callback_data'],'from':{'id':42},'message':{'chat':{'id':42},'message_id':first['message_id']}})
            self.assertEqual(app.models.roles()['main'][0]['model'],'model-010')
            # поиск по части названия — короткий список кнопок
            await tg.commands.dispatch(app,42,env.sid,'model','model-14')
            found=self.sent(env)[-1]
            self.assertIn('найдено',found['text'])
            self.assertEqual(len(found['reply_markup']['inline_keyboard']),tg.commands.PAGE+1)
            await tg.commands.dispatch(app,42,env.sid,'model','model-149')
            self.assertEqual(app.models.roles()['main'][0]['model'],'model-149')
            self.assertEqual([r for r in env.fake.state.requests if r.get('api')=='openai'],[])

    @async_test
    async def test_menu_limits_and_disabled_no_api(self):
        async with MindEnv(self.tmp) as env:
            app=env.app
            with patch('bender.channels.telegram_commands.skills',return_value=[('skill'+str(i),'Навык') for i in range(120)]):
                self.assertEqual(len(menu(app)),100)
                self.assertLess(len(menu(app,True)),10)
            app.settings.set('telegram',{'commands':False})
            self.assertFalse(await app.telegram.commands.sync())
            self.assertEqual(env.fake.state.tg_command_calls,[])


class MaintenanceCommandsTest(TempDirCase):
    """Бекапы, обновление, самопроверка, журнал и нагрузка — из Telegram, без модели."""
    _up=original.TelegramTest._up
    until=original.TelegramTest.until
    sent=original.TelegramTest.sent

    def buttons(self,env):
        return [b for row in self.sent(env)[-1]['reply_markup']['inline_keyboard'] for b in row]

    async def press(self,tg,env,index):
        button=self.buttons(env)[index]
        await tg._callback({'id':'c','data':button['callback_data'],'from':{'id':42},'message':{'chat':{'id':42},'message_id':1}})

    @async_test
    async def test_backup_status_buttons_and_repair(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app;tg=app.telegram
            app.settings.set('backups',{'path':str(self.tmp/'a'),'second_path':str(self.tmp/'b')})
            await tg.commands.dispatch(app,42,env.sid,'backup','')
            text=self.sent(env)[-1]['text']
            self.assertIn('Бекапы включены',text);self.assertIn('ещё не было',text)
            self.assertEqual(len(self.buttons(env)),3)
            await self.press(tg,env,0)  # сохранить сейчас
            self.assertIn('Снимок сохранён',self.sent(env)[-1]['text'])
            await tg.commands.dispatch(app,42,env.sid,'backup','repair')
            self.assertIn('Починка',self.sent(env)[-1]['text'])
            await tg.commands.dispatch(app,42,env.sid,'backup','verify')
            self.assertIn('Всё цело',self.sent(env)[-1]['text'])
            await tg.commands.dispatch(app,42,env.sid,'backup','что-то')
            self.assertIn('/backup repair',self.sent(env)[-1]['text'])

    @async_test
    async def test_update_buttons_install_with_and_without_backup(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app;tg=app.telegram
            plan={'available':True,'head':'a'*40,'target':'b'*40,'branch':'main','commits':['abc123 Починка']}
            install=AsyncMock(return_value={'updated':True,'message':'Программа обновлена.','backup':{'skipped':True,'path':''}})
            with patch.object(app.updater,'check',AsyncMock(return_value=plan)),patch.object(app.updater,'install',install),patch.object(app.updater,'restart',AsyncMock(return_value='Перезапуск.')):
                await tg.commands.dispatch(app,42,env.sid,'update','')
                self.assertIn('Починка',self.sent(env)[-1]['text'])
                labels=[b['text'] for b in self.buttons(env)]
                self.assertEqual(len(labels),3);self.assertIn('без бекапа',labels[1])
                await self.press(tg,env,1)
                install.assert_awaited_once();self.assertTrue(install.await_args.kwargs['skip_backup'])
                self.assertTrue(any('Без бекапа' in m['text'] for m in self.sent(env)))

    @async_test
    async def test_doctor_journal_and_load_answer_without_model(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);app=env.app;tg=app.telegram
            app.journal.write('alarm','Проверочная тревога')
            await tg.commands.dispatch(app,42,env.sid,'journal','alarm')
            self.assertIn('Проверочная тревога',self.sent(env)[-1]['text'])
            await tg.commands.dispatch(app,42,env.sid,'doctor','')
            self.assertIn('Самопроверка',self.sent(env)[-1]['text'])
            await tg.commands.dispatch(app,42,env.sid,'load','')
            text=self.sent(env)[-1]['text']
            self.assertIn('Нагрузка',text);self.assertIn('Безлюдные ходы',text)
            self.assertEqual([r for r in env.fake.state.requests if r.get('api')=='openai'],[])
