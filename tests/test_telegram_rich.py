"""Формат Rich из сохранённой справки; реальные запросы фальшивому Bot API."""
import asyncio
import time
import unittest
from unittest.mock import patch
from bender.channels.telegram import TelegramError
from bender.channels.telegram_rich import normalize,chunks,parts,RichFormatError,LIMIT
from bender.mind.loop import Turn
from bender.mind.passport import brief
from bender.tools.registry import ToolResult
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv,call
from tests import test_telegram as original


class RichFormatTest(unittest.TestCase):
    def test_gfm_table_nested_list_code_currency_formula_hashtag(self):
        source='# Заголовок\n\n| Имя | Цена |\n| --- | --- |\n| А | $5 |\n\n- [ ] раз\n  - вложено\n\n```python\nprint("$5 #tag")\n```\n\nЦена $5 и #заказ. Формула $x^2 + y^2$ и $$E = mc^2$$.'
        formatted=normalize(source)
        self.assertTrue(formatted.startswith('# Заголовок'))
        self.assertIn('| А | \\$5 |',formatted)
        self.assertIn('  - вложено',formatted)
        self.assertIn('print("$5 #tag")',formatted)
        self.assertIn('Цена \\$5 и \\#заказ',formatted)
        self.assertIn('$x^2 + y^2$',formatted)
        self.assertIn('$$E = mc^2$$',formatted)

    def test_chunks_preserve_whole_table_and_original_for_fallback(self):
        table='| Имя | Цена |\n| --- | --- |\n| А | 1 |\n'
        source='а'*20000+'\n\n'+table+'\n'+'б'*20000+'\n\n'+'в'*20000
        split=parts(source)
        self.assertEqual(''.join(raw for raw,_ in split),source)
        self.assertTrue(all(len(text)<=LIMIT for _,text in split))
        self.assertEqual(sum(table.strip() in text for _,text in split),1)
        with self.assertRaises(RichFormatError):chunks('x'*(LIMIT+1))
        with self.assertRaises(RichFormatError):chunks('| '+' | '.join(str(n) for n in range(21))+' |\n| '+' | '.join('---' for _ in range(21))+' |')

    def test_no_automatic_media_buttons_or_thinking_in_final(self):
        text=normalize('![](https://evil.invalid/a.png)\n<img src="https://evil.invalid/b.png">\n<tg-button type="callback_data" data="ap:yes">Да</tg-button>\n<tg-thinking>Секрет</tg-thinking>')
        self.assertNotIn('![](',text)
        self.assertNotIn('<img',text)
        self.assertNotIn('<tg-button',text)
        self.assertNotIn('<tg-thinking>',text)


class RichTelegramTest(TempDirCase):
    _up=original.TelegramTest._up
    until=original.TelegramTest.until
    sent=original.TelegramTest.sent

    @async_test
    async def test_stop_exits_when_api_consumes_cancellation(self):
        from bender.core.app import App
        app=App(self.tmp/'data');app.settings.set('telegram',{'commands':False});tg=app.telegram;entered=asyncio.Event();polls=[];consumed=False
        async def api(method,payload=None,**kw):
            nonlocal consumed
            if method=='getMe':return {'id':1}
            polls.append(method);entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:
                if consumed:raise
                consumed=True
                return []
        try:
            with patch.object(tg,'api',api):
                tg.task=asyncio.create_task(tg._run())
                await entered.wait()
                start=time.monotonic()
                await asyncio.wait_for(tg.stop(),1)
                self.assertLess(time.monotonic()-start,.8)
                self.assertEqual(polls,['getUpdates'])
                self.assertIsNone(tg.task)
        finally:await app.close()

    @async_test
    async def test_thinking_only_in_draft_final_rich_and_live_passport(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set('telegram',{'live_via':'draft'})  # проверяется именно черновик Telegram
            env.fake.state.tg_rich=True
            await self._up(env,owner=True);app=env.app;tg=app.telegram
            candidate=app.router.chain('main')[0]
            self.assertIn('без таблиц и заголовков',brief(app,Turn(env.sid,'telegram'),candidate))
            async def delayed(ctx,items):await asyncio.sleep(1.05);return ToolResult('Лишнее содержимое тула',summary='план готов')
            app.secrets.set('rich-secret','test-rich-private-secret')
            env.script([{'reasoning':'Проверяю test-rich-private-secret https://private.invalid',**call('plan',items=[])},{'text':'# Итог\n\n| Имя | Цена |\n| --- | --- |\n| А | $5 |','delay':.1}])
            with patch.object(app.tools.tools['plan'],'handler',delayed):await app.mind.say(env.sid,'Посмотри',channel='telegram')
            await self.until(lambda:self.sent(env,'sendRichMessage'))
            await self.until(lambda:not tg._bg)
            drafts=self.sent(env,'sendRichMessageDraft')
            self.assertTrue(drafts)
            self.assertTrue(any('<tg-thinking>' in d['rich_message']['markdown'] for d in drafts))
            self.assertNotIn('test-rich-private-secret',str(drafts));self.assertNotIn('private.invalid',str(drafts))
            finals=self.sent(env,'sendRichMessage')
            self.assertEqual(len(finals),1)
            self.assertNotIn('<tg-thinking>',finals[0]['rich_message']['markdown'])
            self.assertIn('| А | \\$5 |',finals[0]['rich_message']['markdown'])
            self.assertEqual(self.sent(env),[])
            self.assertTrue(tg.rich_enabled())
            self.assertIn('Telegram Rich',brief(app,Turn(env.sid,'telegram'),candidate))
            self.assertIn('таблицы до 20 столбцов',brief(app,Turn(env.sid,'telegram'),candidate))
            app.settings.set('telegram',{'rich':False})
            self.assertIn('без таблиц и заголовков',brief(app,Turn(env.sid,'telegram'),candidate))

    @async_test
    async def test_unsupported_cached_and_parse_error_keeps_original_html(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);tg=env.app.telegram;tracked=[];api=tg.api
            async def record(method,payload=None,**kw):tracked.append(method);return await api(method,payload,**kw)
            with patch.object(tg,'api',record):
                await tg.send(42,'Первое **сообщение**')
                await tg.send(42,'Второе')
            self.assertEqual(tracked.count('sendRichMessage'),1)
            self.assertEqual(tracked.count('sendMessage'),2)
            self.assertFalse(tg.rich_enabled())
            env.app.settings.set('telegram',{'api_base':env.base+'/another'})
            self.assertIsNone(tg.capability('sendRichMessage'))
            env.app.settings.set('telegram',{'api_base':env.base+'/tg'})
            tg._capabilities.clear();env.fake.state.tg_rich=True
            async def malformed(method,payload=None,**kw):
                if method=='sendRichMessage':raise TelegramError('tg_api','Telegram: parse rich failed',data={'code':400})
                return await api(method,payload,**kw)
            with patch.object(tg,'api',malformed):await tg.send(42,'Исходный **текст** за $5')
            self.assertEqual(self.sent(env)[-1]['text'],'Исходный <b>текст</b> за $5')
            self.assertIsNone(tg.capability('sendRichMessage'))

    @async_test
    async def test_rate_limit_retries_same_rich_and_oversize_block_uses_html(self):
        async with MindEnv(self.tmp) as env:
            env.fake.state.tg_rich=True
            await self._up(env,owner=True);tg=env.app.telegram;api=tg.api;attempts=[]
            async def limited(method,payload=None,**kw):
                if method=='sendRichMessage':
                    attempts.append(payload)
                    if len(attempts)==1:raise TelegramError('tg_api','Telegram: Too Many Requests',data={'code':429,'retry_after':1})
                return await api(method,payload,**kw)
            with patch.object(tg,'api',limited):await tg.send(42,'# Готово')
            self.assertEqual(attempts[0],attempts[1])
            self.assertEqual(len(self.sent(env,'sendRichMessage')),1)
            await tg.send(42,'x'*(LIMIT+1))
            self.assertGreater(len(self.sent(env)),1)
            self.assertEqual(len(self.sent(env,'sendRichMessage')),1)
